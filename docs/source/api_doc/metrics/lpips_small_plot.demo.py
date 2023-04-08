from plot import image_plot

if __name__ == '__main__':
    image_plot(
        *(f'lpips/{i}.jpg' for i in range(1, 5)),
        save_as='lpips_small.dat.svg',
        columns=2,
        figsize=(6, 6),
    )
