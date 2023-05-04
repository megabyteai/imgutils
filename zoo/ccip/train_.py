import os
import random
import re
from typing import Optional

import torch
from accelerate import Accelerator, DistributedDataParallelKwargs
from ditk import logging
from hbutils.random import global_seed
from sklearn import svm
from sklearn.metrics import accuracy_score
from torch.optim import lr_scheduler
from torch.utils.tensorboard import SummaryWriter
from torchvision.transforms import Compose
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

from .dataset import TRAIN_TRANSFORM, CCIPImagesDataset, CharacterDataset, FastCharacterDataset, TEST_TRANSFORM, char_collect_fn
from .loss import NTXentLoss, MLCELoss
from .model import CCIP
from ..base import _TRAIN_DIR as _GLOBAL_TRAIN_DIR

_TRAIN_DIR = os.path.join(_GLOBAL_TRAIN_DIR, 'ccip')
_LOG_DIR = os.path.join(_TRAIN_DIR, 'logs')
_CKPT_DIR = os.path.join(_TRAIN_DIR, 'ckpts')

_CKPT_PATTERN = re.compile(r'^ccip-(?P<name>[a-zA-Z\d_\-]+)-(?P<epoch>\d+)\.ckpt$')


def _find_latest_ckpt(name: str) -> Optional[str]:
    if os.path.exists(_CKPT_DIR):
        ckpts = []
        for filename in os.listdir(_CKPT_DIR):
            matching = _CKPT_PATTERN.fullmatch(os.path.basename(filename))
            if matching and matching.group('name') == name:
                ckpts.append((int(matching.group('epoch')), os.path.join(_CKPT_DIR, filename)))

        ckpts = sorted(ckpts)
        if ckpts:
            return ckpts[-1][1]
        else:
            return None
    else:
        return None


def _ckpt_epoch(filename: Optional[str]) -> Optional[int]:
    if filename is not None:
        matching = _CKPT_PATTERN.fullmatch(os.path.basename(filename))
        if matching:
            return int(matching.group('epoch'))
        else:
            return None
    else:
        return None


def _sample_analysis(poss, negs, svm_samples: int = 10000):
    poss_cnt, negs_cnt = poss.shape[0], negs.shape[0]
    total = poss_cnt + negs_cnt
    if total > svm_samples:
        s_poss = poss[random.sample(range(poss_cnt), k=int(round(poss_cnt * svm_samples / total)))]
        s_negs = negs[random.sample(range(negs_cnt), k=int(round(negs_cnt * svm_samples / total)))]
    else:
        s_poss, s_negs = poss, negs

    s_poss, s_negs = s_poss.cpu(), s_negs.cpu()
    features = torch.cat([s_poss, s_negs]).detach().numpy()
    labels = torch.cat([torch.ones_like(s_poss), -torch.ones_like(s_negs)]).detach().numpy()

    model = svm.SVC(kernel='linear')  # 线性核
    model.fit(features.reshape(-1, 1), labels)
    predictions = model.predict(features.reshape(-1, 1))

    coef = model.coef_.reshape(-1)[0].tolist()
    inter = model.intercept_.reshape(-1)[0].tolist()
    threshold = -inter / coef

    return poss.mean().item(), poss.std().item(), negs.mean().item(), negs.std().item(), \
           threshold, accuracy_score(labels, predictions)


def train(dataset_dir: str, session_name: Optional[str] = None, from_ckpt: Optional[str] = None,
          train_ratio: float = 0.8, max_epochs: int = 500, group_size: int = 30,
          learning_rate: float = 0.001, weight_decay: float = 1e-2, tau: float = 0.15,
          save_per_epoch: int = 10, eval_epoch: int = 5, num_workers=8,
          model_name: str = 'clip/ViT-B/32', seed: Optional[int] = 0):
    if seed is not None:
        # native random, numpy, torch and faker's seeds are includes
        # if you need to register more library for seeding, see:
        # https://hansbug.github.io/hbutils/main/api_doc/random/state.html#register-random-source
        global_seed(seed)

    ddp_kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(
        # mixed_precision=self.cfgs.mixed_precision,
        step_scheduler_with_optimizer=False,
        kwargs_handlers=[ddp_kwargs],
    )

    session_name = session_name or re.sub(r'\W+', '-', model_name)
    _log_dir = os.path.join(_LOG_DIR, session_name)

    if accelerator.is_local_main_process:
        os.makedirs(_log_dir, exist_ok=True)
        os.makedirs(_CKPT_DIR, exist_ok=True)
        writer = SummaryWriter(_log_dir)
        writer.add_custom_scalars({
            "contrastive": {
                "train": ["Multiline", ["train/pos/mean", "train/neg/mean"]],
                "test": ["Multiline", ["test/pos/mean", "test/neg/mean"]],
            },
        })
    else:
        writer = None

    model = CCIP(model_name)
    image_dataset = CCIPImagesDataset(dataset_dir)
    train_image_dataset, test_image_dataset = image_dataset.split_dataset(test_prob=1 - train_ratio,
                                    train_transform=Compose(TRAIN_TRANSFORM+model.preprocess),
                                    test_transform=Compose(TEST_TRANSFORM+model.preprocess),)

    train_dataset = FastCharacterDataset(train_image_dataset, group_size, force_prob=False)
    test_dataset = FastCharacterDataset(test_image_dataset, group_size)
    train_dataset.reset()
    test_dataset.reset()
    train_dataloader = DataLoader(train_dataset, batch_size=group_size, shuffle=True, num_workers=num_workers, collate_fn=char_collect_fn,
                                  drop_last=True)
    test_dataloader = DataLoader(test_dataset, batch_size=group_size, num_workers=num_workers, collate_fn=char_collect_fn)

    if from_ckpt is None:
        from_ckpt = _find_latest_ckpt(session_name)
    previous_epoch = _ckpt_epoch(from_ckpt) or 0
    if from_ckpt:
        logging.info(f'Load checkpoint from {from_ckpt!r}.')
        model.load_state_dict(torch.load(from_ckpt, map_location='cpu'))
    else:
        logging.info(f'No checkpoint found, new model will be used.')

    loss_fn = MLCELoss().to(accelerator.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    scheduler = lr_scheduler.OneCycleLR(
        optimizer, max_lr=learning_rate,
        steps_per_epoch=len(train_dataset), epochs=max_epochs,
        pct_start=0.15, final_div_factor=20.
    )

    model, optimizer, train_dataloader, test_dataloader, scheduler = \
        accelerator.prepare(model, optimizer, train_dataloader, test_dataloader, scheduler)

    for epoch in range(previous_epoch + 1, max_epochs + 1):
        running_loss = 0.0
        train_pos_total = 0
        positive_sims, negative_sims = [], []
        model.train()
        for i, (inputs, char_ids) in enumerate(tqdm(train_dataloader)):
            train_dataloader.dataset.reset()
            inputs = inputs.to(accelerator.device)  # BxCxHxW
            char_ids = char_ids.to(accelerator.device)  # B

            # B = len(char_ids)
            # mask = torch.triu(torch.ones(B,B),diagonal=1).to(accelerator.device)  # BxB, remove duplicated
            # similarities = model(inputs)  # BxB
            # outputs = similarities[mask]  # N
            # labels = (char_ids.view(-1,1) == char_ids.view(1,-1))[mask]  # N
            # labels = char_ids

            outputs = model(inputs)  # BxB
            labels = char_ids

            loss = loss_fn(outputs, labels)
            accelerator.backward(loss)
            optimizer.step()
            scheduler.step()

            running_loss += loss.item()*len(char_ids)

            gt = (char_ids.view(-1, 1) == char_ids.view(1, -1)).detach().cpu()
            outputs = outputs.detach().cpu()
            gt_diag0 = gt.clone()
            gt_diag0.diagonal().copy_(torch.zeros(len(char_ids)))
            # outputs.diagonal().copy_(torch.ones(len(char_ids))*-10000)
            # max_idxs = outputs.argsort(dim=-1)
            # for max_idx, n_pos in zip(max_idxs, gt.sum(dim=1)):
            #     train_pos_total += n_pos
            #     positive_sims.append(outputs[labels])
            #     negative_sims.append(outputs[~labels])
            train_pos_total += gt_diag0.sum()
            positive_sims.append(outputs[gt_diag0])
            negative_sims.append(outputs[~gt])

        epoch_loss = running_loss #/ train_pos_total
        train_psims = torch.cat(positive_sims)
        train_nsims = torch.cat(negative_sims)
        train_pos_mean, train_pos_std, train_neg_mean, train_neg_std, train_threshold, train_acc_svm = \
            _sample_analysis(train_psims, train_nsims)

        if accelerator.is_local_main_process:
            logging.info(f'Epoch [{epoch}/{max_epochs}], loss: {epoch_loss:.6f}, '
                         f'acc_svm: {train_acc_svm:.6f}, threshold: {train_threshold:.6f}.')
            if writer:
                writer.add_scalar('train/loss', epoch_loss, epoch)
                writer.add_scalar('train/pos/mean', train_pos_mean, epoch)
                writer.add_scalar('train/pos/std', train_pos_std, epoch)
                writer.add_scalar('train/neg/mean', train_neg_mean, epoch)
                writer.add_scalar('train/neg/std', train_neg_std, epoch)
                writer.add_scalar('train/threshold', train_threshold, epoch)
                writer.add_scalar('train/acc_svm', train_acc_svm, epoch)

        model.eval()
        if epoch % eval_epoch == 0:
            with torch.no_grad():
                positive_sims, negative_sims = [], []
                for i, (inputs, char_ids) in enumerate(tqdm(test_dataloader)):
                    inputs = inputs.to(accelerator.device)  # BxCxHxW
                    char_ids = char_ids.to(accelerator.device)  # B

                    outputs = model(inputs)  # BxB

                    gt = (char_ids.view(-1, 1) == char_ids.view(1, -1)).detach().cpu()
                    outputs = outputs.detach().cpu()
                    gt_diag0 = gt.clone()
                    gt_diag0.diagonal().copy_(torch.zeros(len(char_ids)))
                    train_pos_total += gt_diag0.sum()
                    positive_sims.append(outputs[gt_diag0])
                    negative_sims.append(outputs[~gt])

                test_psims = torch.cat(positive_sims)
                test_nsims = torch.cat(negative_sims)
                test_pos_mean, test_pos_std, test_neg_mean, test_neg_std, test_threshold, test_acc_svm = \
                    _sample_analysis(test_psims, test_nsims)

                if accelerator.is_local_main_process:
                    logging.info(f'Epoch {epoch}, '
                                 f'acc_svm: {test_acc_svm:.6f}, threshold: {test_threshold:.6f}')
                    if writer:
                        writer.add_scalar('test/pos/mean', test_pos_mean, epoch)
                        writer.add_scalar('test/pos/std', test_pos_std, epoch)
                        writer.add_scalar('test/neg/mean', test_neg_mean, epoch)
                        writer.add_scalar('test/neg/std', test_neg_std, epoch)
                        writer.add_scalar('test/threshold', test_threshold, epoch)
                        writer.add_scalar('test/acc_svm', test_acc_svm, epoch)

        if accelerator.is_local_main_process and epoch % save_per_epoch == 0:
            current_ckpt_file = os.path.join(_CKPT_DIR, f'ccip-{session_name}-{epoch}.ckpt')
            torch.save(model.state_dict(), current_ckpt_file)
            logging.info(f'Saved to {current_ckpt_file!r}.')
