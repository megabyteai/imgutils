import pytest
from PIL import Image

from imgutils.metrics import psnr
from imgutils.upscale import upscale_with_cdc


@pytest.mark.unittest
class TestUpscaleCDC:
    def test_upscale_with_cdc_4x(self, sample_image):
        assert psnr(
            upscale_with_cdc(sample_image),
            sample_image.resize((sample_image.width * 4, sample_image.height * 4), Image.LANCZOS)
        ) >= 34.5

    def test_upscale_with_cdc_2x(self, sample_image):
        assert psnr(
            upscale_with_cdc(sample_image, model='HGSR-MHR_X2_1680'),
            sample_image.resize((sample_image.width * 2, sample_image.height * 2), Image.LANCZOS)
        ) >= 35.5

    def test_upscale_with_cdc_small_4x(self, sample_image_small, sample_image):
        assert psnr(
            upscale_with_cdc(sample_image_small)
            .resize(sample_image.size, Image.LANCZOS),
            sample_image,
        ) >= 28.5

    def test_upscale_with_cdc_small_2x(self, sample_image_small, sample_image):
        assert psnr(
            upscale_with_cdc(sample_image_small, model='HGSR-MHR_X2_1680')
            .resize(sample_image.size, Image.LANCZOS),
            sample_image,
        ) >= 28.0
