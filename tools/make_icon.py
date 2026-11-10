"""
Draws Water Column Plotter's application icon: the pie (wedge) display of a multibeam swath, in Viridis colors,
on a dark rounded tile. Writes WaterColumnPlotter/Web/static/wcp.ico (16 to 256 pixels) and wcp-256.png.

Run from the repository's top folder with:

    uv run --no-project --with numpy --with pillow python tools/make_icon.py
"""
from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path(__file__).resolve().parent.parent / 'WaterColumnPlotter' / 'Web' / 'static'
N = 1024  # drawn this size, then scaled down, which smooths the edges
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

VIRIDIS = np.array([[0x44, 0x01, 0x54], [0x48, 0x28, 0x78], [0x3b, 0x52, 0x8b], [0x2c, 0x72, 0x8e],
                    [0x21, 0x91, 0x8c], [0x28, 0xae, 0x80], [0x5e, 0xc9, 0x62], [0xad, 0xdc, 0x30],
                    [0xfd, 0xe7, 0x25]], float)
TILE = np.array([0x1b, 0x22, 0x30], float)
EDGE = np.array([0xd8, 0xe0, 0xea], float)


def viridis(v):
    v = np.clip(v, 0, 1) * (len(VIRIDIS) - 1)
    i = np.minimum(v.astype(int), len(VIRIDIS) - 2)
    f = (v - i)[..., None]
    return VIRIDIS[i] * (1 - f) + VIRIDIS[i + 1] * f


def draw():
    y, x = (np.mgrid[0:N, 0:N] + 0.5) / N
    rgb = np.zeros((N, N, 3)) + TILE

    # The tile: a square with rounded corners.
    m, rad = 0.03, 0.2
    cx, cy = np.clip(x, m + rad, 1 - m - rad), np.clip(y, m + rad, 1 - m - rad)
    alpha = (np.hypot(x - cx, y - cy) <= rad).astype(float)

    # The swath: beams fan out from the transducer at the top, down to a flat seafloor.
    ax, ay, floor, half_angle = 0.5, 0.25, 0.70, np.radians(44)
    depth = floor - ay
    dx, dy = x - ax, y - ay
    r, angle = np.hypot(dx, dy), np.arctan2(dx, dy)
    wedge = (np.abs(angle) <= half_angle) & (dy >= 0) & (y <= floor)

    rng = np.random.default_rng(3)
    speckle = rng.normal(0, 1, (128, 128)).repeat(N // 128, 0).repeat(N // 128, 1)

    v = 0.06 + 0.03 * speckle                                            # quiet water near the transducer
    v += 0.40 * np.exp(-((dy / depth - 0.5) / 0.07) ** 2)                # a scattering layer
    v = np.where(r > depth, 0.82 + 0.06 * speckle, v)                    # sidelobe returns beyond the nearest seafloor
    v = np.maximum(v, np.exp(-((r - depth) / 0.022) ** 2))               # the bright arc of the nearest seafloor
    v = np.maximum(v, np.exp(-((y - floor) / 0.02) ** 2))                # the seafloor itself
    rgb[wedge] = viridis(v)[wedge]

    # Light outlines along the outer beams, so the wedge reads at small sizes.
    side = np.abs(r * np.sin(np.abs(angle) - half_angle))
    outline = (side < 0.012) & (dy >= 0) & (y <= floor + 0.006) & (np.abs(angle) <= half_angle + 0.2)
    rgb[outline] = EDGE

    rgba = np.dstack([rgb, alpha * 255]).round().astype(np.uint8)
    return Image.fromarray(rgba, 'RGBA')


def main():
    big = draw()
    images = [big.resize((s, s), Image.LANCZOS) for s in SIZES]
    # Classic bitmaps rather than Pillow's default PNGs: parts of Windows (Search's preview, for one) don't show
    # PNG-compressed icon images.
    images[-1].save(OUT / 'wcp.ico', sizes=[(s, s) for s in SIZES], append_images=images[:-1], bitmap_format='bmp')
    images[-1].save(OUT / 'wcp-256.png')
    print(f"Wrote {OUT / 'wcp.ico'} and {OUT / 'wcp-256.png'}")


if __name__ == '__main__':
    main()
