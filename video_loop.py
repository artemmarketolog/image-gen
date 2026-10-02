#!/usr/bin/env python3
"""
video_loop — бесшовная петля для сайта из сырого ролика видео-модели и файлы для публикации.

  video_loop.py RAW.mp4 --name coin --out-dir <сайт>/public/assets [--mode pingpong|forward]
                [--trim 0 120] [--ease 12] [--bg '#ff5715'] [--size 1280] [--av1-crf 36] [--h264-crf 24]

pingpong  ролик вперёд и обратно без повторов кадров на разворотах: полуоборот, покачивание, переход A→B→A.
forward   ролик уже зациклен (первый кадр = последнему): последний кадр-дубль выбрасывается.
--ease N  первые N кадров растягиваются в плавный старт. Нужен, если модель движется с первого кадра:
          иначе на развороте pingpong виден рывок. Растянутые кадры смешиваются попарно.
--bg      фон каждого кадра подтягивается к цвету блока сайта (ролик снят на ровном фоне этого цвета).
Выход в --out-dir, имена с хешем содержимого: <name>-av1.<hash>.mp4 (основной), <name>-h264.<hash>.mp4
(запасной для Safari без AV1), <name>-poster.<hash>.webp (первый кадр). В конце печатает готовый <video>.
Размер --size: ширина показа на десктопе × 2 (Retina), увеличение lanczos. Подробно: references/video.md.
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

BT709 = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv']


def loop_positions(kept: int, mode: str = 'pingpong', ease: int = 0) -> list[float]:
    """Позиции исходных кадров для петли. Дробная позиция = смесь двух соседних кадров."""
    ease = min(ease, kept - 1)
    forward = [ease * (k / (2 * ease)) ** 2 for k in range(2 * ease)] + list(range(ease, kept)) if ease else list(range(kept))
    return forward + forward[-2:0:-1] if mode == 'pingpong' else forward


def flatten_bg(frame: np.ndarray, color: np.ndarray) -> np.ndarray:
    """Подтягивает фон кадра к цвету блока: сдвигаются только пиксели, близкие к медиане рамки кадра."""
    h = frame.shape[0]
    edge = max(8, h // 24)
    ring = np.concatenate([frame[:edge].reshape(-1, 3), frame[-edge:].reshape(-1, 3),
                           frame[:, :edge].reshape(-1, 3), frame[:, -edge:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    weight = np.clip(1 - np.linalg.norm(frame - bg, axis=2) / 30, 0, 1)[..., None]
    return frame + weight * (color - bg)


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=width,height,r_frame_rate', '-of', 'csv=p=0', str(path)],
                         capture_output=True, text=True, check=True).stdout.strip().split(',')
    num, den = out[2].split('/')
    return int(out[0]), int(out[1]), float(num) / float(den)


def build_master(raw: Path, master: Path, a) -> tuple[int, float, float]:
    """Петля без потерь (RGB). Возвращает число кадров, fps и шов: разницу последнего кадра с первым / средний шаг."""
    w, h, fps = probe(raw)
    color = np.array([int(a.bg.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4)], np.float32) if a.bg else None
    start, end = a.trim if a.trim else (0, 10 ** 9)
    dec = subprocess.Popen(['ffmpeg', '-v', 'error', '-i', str(raw), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                           stdout=subprocess.PIPE)
    store = master.with_suffix('.frames')
    kept, index = 0, 0
    with open(store, 'wb') as out:
        while True:
            buf = dec.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            if start <= index <= end:
                frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                if color is not None:
                    frame = np.clip(flatten_bg(frame.astype(np.float32), color).round(), 0, 255).astype(np.uint8)
                out.write(frame.tobytes())
                kept += 1
            index += 1
    dec.wait()
    frames = np.memmap(store, np.uint8, 'r', shape=(kept, h, w, 3))
    if a.mode == 'forward' and kept > 2 and np.abs(frames[-1].astype(np.int16) - frames[0]).mean() < 1.0:
        kept -= 1
    order = loop_positions(kept, a.mode, a.ease)

    def at(pos):
        i, t = int(pos), pos - int(pos)
        if t < 1e-6:
            return np.asarray(frames[i])
        return (frames[i] * (1 - t) + frames[min(i + 1, kept - 1)] * t).round().astype(np.uint8)

    enc = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}',
                            '-r', str(fps), '-i', '-', '-an', '-c:v', 'libx264rgb', '-qp', '0', '-preset', 'ultrafast',
                            str(master)], stdin=subprocess.PIPE)
    steps, prev = [], None
    for pos in order:
        frame = at(pos)
        if prev is not None and len(steps) < 400:
            steps.append(float(np.abs(frame.astype(np.int16) - prev).mean()))
        prev = frame.astype(np.int16)
        enc.stdin.write(np.ascontiguousarray(frame).tobytes())
    enc.stdin.close()
    enc.wait()
    seam = float(np.abs(prev - at(order[0]).astype(np.int16)).mean())
    del frames
    store.unlink()
    return len(order), fps, seam / (np.median(steps) or 1)


def encode(master: Path, a, tmp: Path) -> dict:
    vf = f'scale={a.size}:-2:flags=lanczos:out_color_matrix=bt709:out_range=tv,format=yuv420p'
    files = {'av1': tmp / 'av1.mp4', 'h264': tmp / 'h264.mp4', 'poster': tmp / 'poster.webp'}
    run = lambda *args: subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(master), *args], check=True,
                                       stderr=subprocess.DEVNULL if 'libsvtav1' in args else None)
    run('-vf', vf, '-c:v', 'libsvtav1', '-crf', str(a.av1_crf), '-preset', '4', '-g', '300', *BT709,
        '-movflags', '+faststart', '-an', str(files['av1']))
    run('-vf', vf, '-c:v', 'libx264', '-crf', str(a.h264_crf), '-preset', 'veryslow', '-profile:v', 'high', *BT709,
        '-movflags', '+faststart', '-an', str(files['h264']))
    run('-vf', f"select='eq(n\\,0)',scale={a.size}:-2:flags=lanczos", '-frames:v', '1', '-c:v', 'libwebp',
        '-quality', '84', str(files['poster']))
    return files


def av1_codec(path: Path) -> str:
    level = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=level',
                            '-of', 'csv=p=0', str(path)], capture_output=True, text=True).stdout.strip()
    return f'av01.0.{int(level or 8):02d}M.08'


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Бесшовная видео-петля для сайта: AV1 + H.264 + постер')
    p.add_argument('raw', type=Path)
    p.add_argument('--name', required=True, help='префикс файлов, например coin')
    p.add_argument('--out-dir', type=Path, required=True, help='куда положить файлы, обычно public/assets сайта')
    p.add_argument('--mode', choices=('pingpong', 'forward'), default='pingpong')
    p.add_argument('--trim', type=int, nargs=2, metavar=('START', 'END'), help='оставить кадры START..END включительно')
    p.add_argument('--ease', type=int, default=0, help='сколько кадров растянуть в плавный старт')
    p.add_argument('--bg', help='цвет блока #rrggbb, к которому подтянуть фон')
    p.add_argument('--size', type=int, default=1280, help='ширина файла: ширина показа на десктопе × 2')
    p.add_argument('--av1-crf', type=int, default=36)
    p.add_argument('--h264-crf', type=int, default=24)
    p.add_argument('--keep-master', type=Path, help='сохранить мастер без потерь сюда')
    a = p.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        master = a.keep_master or tmp / 'master.mkv'
        count, fps, seam = build_master(a.raw, master, a)
        files = encode(master, a, tmp)
        names = {}
        for kind, path in files.items():
            ext = 'webp' if kind == 'poster' else 'mp4'
            names[kind] = f'{a.name}-{kind}.{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}.{ext}'
            shutil.copyfile(path, a.out_dir / names[kind])
        codec = av1_codec(a.out_dir / names['av1'])
        out_w, out_h, _ = probe(a.out_dir / names['av1'])
    print(f'Петля: {count} кадров, {count / fps:.2f} с при {fps:g} fps; шов {seam:.2f} от обычного шага (норма ≤ 1.5)')
    for kind in ('av1', 'h264', 'poster'):
        print(f'  {names[kind]}  {(a.out_dir / names[kind]).stat().st_size / 1024:.0f} КБ')
    print('\n<video data-lazy-video muted playsinline loop preload="none" aria-hidden="true" disablepictureinpicture '
          f'disableremoteplayback poster="/assets/{names["poster"]}" width="{out_w}" height="{out_h}">\n'
          f' <source data-src="/assets/{names["av1"]}" type="video/mp4; codecs={codec}">\n'
          f' <source data-src="/assets/{names["h264"]}" type="video/mp4">\n</video>\n'
          'Скрипт ленивой загрузки и CSS: references/video.md.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
