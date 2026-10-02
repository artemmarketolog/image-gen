#!/usr/bin/env python3
"""
upscale.py — апскейл существующей картинки через Real-ESRGAN x4 (CPU).

Не генерация: ни один пиксель не придумывается заново. Лицо, текст и композиция
остаются те же, снимается шум и мыло. Ровно то, что нужно, когда клиент жалуется
на качество готового креатива.

Зависимости: pip install -r requirements-upscale.txt (torch CPU + spandrel), см. README.

  python upscale.py --input ref.png --output out.png --size 2160x3840

Без --size кладёт чистый x4. Считает на CPU: 1.5 Мпикс ≈ 20 минут на 6 ядрах.
"""
import argparse
import re
import urllib.request
from pathlib import Path
from skill_config import CACHE_DIR

import numpy as np
import torch
from PIL import Image
from spandrel import ModelLoader

WEIGHTS = CACHE_DIR / "models" / "RealESRGAN_x4plus.pth"
WEIGHTS_URL = ("https://github.com/xinntao/Real-ESRGAN/releases/download/"
               "v0.1.0/RealESRGAN_x4plus.pth")


def upscale(img: Image.Image, tile: int, pad: int) -> Image.Image:
    """x4 по тайлам с перекрытием — иначе не хватит памяти на крупном кадре."""
    model = ModelLoader().load_from_file(str(WEIGHTS)).eval()
    w, h = img.size
    arr = np.asarray(img.convert("RGB")).astype(np.float32) / 255.0
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
    out = torch.zeros(1, 3, h * 4, w * 4)

    tiles_x = (w + tile - 1) // tile
    tiles_y = (h + tile - 1) // tile
    done = 0
    with torch.no_grad():
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                x0, y0 = tx * tile, ty * tile
                x1, y1 = min(x0 + tile, w), min(y0 + tile, h)
                px0, py0 = max(x0 - pad, 0), max(y0 - pad, 0)
                px1, py1 = min(x1 + pad, w), min(y1 + pad, h)
                up = model(t[:, :, py0:py1, px0:px1])
                cx0, cy0 = (x0 - px0) * 4, (y0 - py0) * 4
                out[:, :, y0 * 4:y1 * 4, x0 * 4:x1 * 4] = up[
                    :, :, cy0:cy0 + (y1 - y0) * 4, cx0:cx0 + (x1 - x0) * 4
                ]
                done += 1
                print(f"  тайл {done}/{tiles_x * tiles_y}", flush=True)

    res = out.clamp(0, 1).squeeze(0).permute(1, 2, 0).numpy()
    return Image.fromarray((res * 255.0).round().astype(np.uint8))


def fit(img: Image.Image, target: str) -> Image.Image:
    """Приводит к точному ШxВ: вписывает по ширине и режет по центру."""
    tw, th = (int(v) for v in target.lower().split("x"))
    img = img.resize((tw, round(tw * img.height / img.width)), Image.LANCZOS)
    if img.height != th:
        top = (img.height - th) // 2
        img = img.crop((0, top, tw, top + th))
    return img


def main():
    ap = argparse.ArgumentParser(description="Апскейл картинки через Real-ESRGAN x4")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--size", default=None, help="целевой ШxВ, например 2160x3840")
    ap.add_argument("--tile", type=int, default=192)
    ap.add_argument("--threads", type=int, default=6)
    args = ap.parse_args()

    if args.size and not re.fullmatch(r"\d+x\d+", args.size):
        raise SystemExit(f"--size ждёт ШxВ, получил: {args.size}")

    if not WEIGHTS.exists():
        WEIGHTS.parent.mkdir(parents=True, exist_ok=True)
        print(f"Качаю веса в {WEIGHTS}...", flush=True)
        urllib.request.urlretrieve(WEIGHTS_URL, WEIGHTS)

    torch.set_num_threads(args.threads)
    src = Image.open(args.input)
    print(f"Вход: {src.size[0]}x{src.size[1]}", flush=True)

    big = upscale(src, args.tile, args.tile // 8)
    print(f"После x4: {big.size[0]}x{big.size[1]}", flush=True)
    if args.size:
        big = fit(big, args.size)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    big.save(args.output, "PNG", optimize=True)
    print(f"✓ Готово: {args.output} ({big.size[0]}x{big.size[1]})")


if __name__ == "__main__":
    main()
