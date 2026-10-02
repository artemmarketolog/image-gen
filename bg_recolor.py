#!/usr/bin/env python3
"""
bg_recolor — перекрашивает ровный однотонный фон кадра в точный цвет (обычно цвет блока сайта), не трогая объект.

  bg_recolor.py INPUT OUTPUT --color '#ff5715' [--size 1280]   # --size: ширина результата

Нужен перед видео-моделью: если фон кадра совпадает с заливкой блока, видео растворяется в странице без рамки.
Фон отделяется заливкой от краёв кадра по близости к медиане рамки, край объекта смешивается заново:
new = old + (1 − a) · (новый фон − старый фон). Внутренние области объекта того же цвета не трогаются,
если объект их замыкает (например, кольцо монеты).
"""
import argparse
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def recolor(image: Image.Image, color: str) -> tuple[Image.Image, np.ndarray, tuple[int, int, int, int]]:
    rgb = np.asarray(image.convert('RGB')).astype(float)
    h, w, _ = rgb.shape
    edge = max(8, h // 16)
    border = np.concatenate([rgb[:edge].reshape(-1, 3), rgb[-edge:].reshape(-1, 3),
                             rgb[:, :edge].reshape(-1, 3), rgb[:, -edge:].reshape(-1, 3)])
    bg_old = np.median(border, axis=0)
    bg_new = np.array([int(color.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4)], float)
    dist = np.linalg.norm(rgb - bg_old, axis=2)
    # fromarray отдаёт картинку только для чтения: floodfill по ней молча ничего не пишет, отсюда copy()
    near = Image.fromarray(np.where(dist < 30, 255, 0).astype(np.uint8)).copy()
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if near.getpixel(xy) == 255:
            ImageDraw.floodfill(near, xy, 128)
    outer = np.asarray(near) == 128
    band = np.asarray(Image.fromarray((outer * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))) > 0
    alpha = np.ones((h, w))
    alpha[band] = np.clip((dist[band] - 8) / 42, 0, 1)
    out = rgb + (1 - alpha)[..., None] * (bg_new - bg_old)
    out[outer & (dist < 8)] = bg_new
    ys, xs = np.where(alpha > 0.5)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(xs) else (0, 0, w - 1, h - 1)
    return Image.fromarray(np.clip(out.round(), 0, 255).astype(np.uint8)), bg_old, bbox


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Перекрасить ровный фон кадра в точный цвет')
    p.add_argument('input')
    p.add_argument('output')
    p.add_argument('--color', required=True, help='#rrggbb')
    p.add_argument('--size', type=int, help='ширина результата, пропорции сохраняются (lanczos)')
    a = p.parse_args(argv)
    image = Image.open(a.input)
    if a.size:
        image = image.convert('RGB').resize((a.size, round(image.height * a.size / image.width)), Image.LANCZOS)
    result, bg_old, bbox = recolor(image, a.color)
    result.save(a.output)
    w, h = result.size
    print(f'{a.output}: фон {bg_old.round().astype(int).tolist()} → {a.color}; объект {bbox}, '
          f'занимает {((bbox[2] - bbox[0]) / w):.0%} ширины кадра (для мягкой маски края нужно ≤ 65%)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
