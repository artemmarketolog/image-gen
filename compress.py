#!/usr/bin/env python3
"""Fast delivery compression and durable storage; never modifies the input file."""
import argparse
import io
import tempfile
import time
from pathlib import Path
from skill_config import DATA_DIR

from PIL import Image, ImageOps

STATE_DIR = DATA_DIR


def archive_dir(project=None):
    if project:
        relative = Path(project)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('project must be a relative client/task path')
        return STATE_DIR / 'creatives' / relative / 'images'
    return STATE_DIR / 'image-gen' / 'images' / time.strftime('%Y-%m')


def save_compressed(data, name, project=None, target=None):
    """Archive first, then compress. On compression failure retain the paid image."""
    root = archive_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='image-', dir=root))
    stem = Path(name).stem or 'image'
    original = folder / (stem + '.png')
    original.write_bytes(data)
    start = time.perf_counter()
    try:
        with Image.open(io.BytesIO(data)) as source:
            suffix = { 'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp' }.get(source.format, '.bin')
            actual = original.with_suffix(suffix)
            if actual != original:
                original = original.rename(actual)
            im = ImageOps.exif_transpose(source)
            alpha = 'A' in im.getbands() or 'transparency' in im.info
            im = im.convert('RGBA' if alpha else 'RGB')
            if target:
                im.thumbnail(target, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            profile = source.info.get('icc_profile')
            opts = {'icc_profile': profile} if profile else {}
            if alpha:
                im.save(buffer, 'PNG', compress_level=3, **opts)
                extension = '.png'
            else:
                im.save(buffer, 'JPEG', quality=95, subsampling=0, **opts)
                extension = '.jpg'
            encoded = buffer.getvalue()
            if len(encoded) < len(data):
                result = folder / (stem + extension)
                temporary = folder / 'encoded.tmp'
                temporary.write_bytes(encoded)
                temporary.replace(result)
                if result != original:
                    original.unlink()
            else:
                result = original
        print(f'  сжатие: {len(data)//1024} → {result.stat().st_size//1024} КБ; '
              f'{time.perf_counter()-start:.3f} с; архив: {result}')
        return result
    except Exception as exc:
        print(f'  сжатие не выполнено ({exc}); исходник сохранён: {original}')
        return original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--project')
    args = parser.parse_args()
    source = Path(args.input)
    print(save_compressed(source.read_bytes(), source.name, args.project))


if __name__ == '__main__':
    main()
