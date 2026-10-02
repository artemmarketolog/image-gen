#!/usr/bin/env python3
"""
video_generate — видео из картинки через laozhang по первому и, по желанию, последнему кадру.
Модель всегда Seedance 2.0 (лучшее качество в наших тестах); Wan 2.7 (--model wan) только по явной просьбе.

Одна команда create = одна платная генерация. Повтор с тем же --name запрещён, пока жив его job-файл:
случайный перезапуск не спишет деньги второй раз. Новая генерация = новое имя и явное «да» пользователя.
Каждый запрос пишется в ledger до отправки; после отправки автоповторов нет.

  create --name N --first КАДР [--last КАДР] --prompt ТЕКСТ [--dry-run] [--model wan] [...]
  poll   --name N     бесплатно ждёт готовности, кладёт raw.mp4 в архив, удаляет временные входные кадры
  status --name N

КАДР: https-ссылка как есть или локальный файл. Модели работают на чужих серверах и скачивают кадр по ссылке, наш
сервер они не видят: локальный файл выкладывается в вашу публичную папку IMAGE_GEN_PUBLIC_DIR (адрес IMAGE_GEN_PUBLIC_URL)
под случайным именем и удаляется, когда задача завершилась. Без этой настройки передавайте https-ссылки.
Ключи: LAOZHANG_WAN_API_KEY и LAOZHANG_SEEDANCE_API_KEY в ~/.config/media-skills/image-gen.env; токены групп Wan и SeeDance2
должны быть с оплатой по объёму (pay-as-you-go), иначе 503 «under billing mode [pay-per-request]».
Подробно: references/video.md.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from skill_config import DATA_DIR, ENV_FILE, setting

BASE_URL = 'https://api2.laozhang.ai'
CREDENTIALS = ENV_FILE
STATE_DIR = DATA_DIR
PUBLIC_DIR = Path(setting('IMAGE_GEN_PUBLIC_DIR')).expanduser() if setting('IMAGE_GEN_PUBLIC_DIR') else None
PUBLIC_URL = setting('IMAGE_GEN_PUBLIC_URL', '')

MODELS = {
    'wan': {
        'id': 'wan2.7-i2v', 'key': 'LAOZHANG_WAN_API_KEY', 'group': 'Wan',
        'create': '/wan/api/v1/services/aigc/video-generation/video-synthesis', 'poll': '/v1/tasks/{id}',
        'durations': range(2, 16), 'resolutions': ('720p', '1080p'),
    },
    'seedance': {
        'id': 'doubao-seedance-2-0-260128', 'key': 'LAOZHANG_SEEDANCE_API_KEY', 'group': 'SeeDance2',
        'create': '/seedance/api/v3/contents/generations/tasks', 'poll': '/seedance/api/v3/contents/generations/tasks/{id}',
        'durations': range(4, 16), 'resolutions': ('480p', '720p', '1080p'),
    },
}
# Wan: 0.6 / 1 RMB за секунду (720P / 1080P) × коэффициент группы 0.15. Seedance: токены ≈ сек × площадь × 24 / 1024,
# 46 RMB за 1M токенов × коэффициент SeeDance2 0.18. Факт 21.09.2026: 5 с 720p = $0.45 (Wan) и $0.90 (Seedance).
WAN_USD_PER_SEC = {'720p': 0.09, '1080p': 0.15}
SEEDANCE_PIXELS = {'480p': 409_600, '720p': 921_600, '1080p': 2_073_600}
SEEDANCE_USD_PER_MTOK = 46 * 0.18
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')


def state(*parts) -> Path:
    return STATE_DIR.joinpath('video-gen', *parts)


def job_path(name: str) -> Path:
    return state('jobs', name + '.json')


def estimate_usd(model: str, duration: int, resolution: str) -> float:
    if model == 'wan':
        return round(WAN_USD_PER_SEC[resolution] * duration, 2)
    tokens = duration * SEEDANCE_PIXELS[resolution] * 24 / 1024
    return round(tokens / 1e6 * SEEDANCE_USD_PER_MTOK, 2)


def build_request(model, first, last, prompt, negative=None, duration=5, resolution='720p', ratio=None,
                  seed=None, audio=False):
    """Тело и заголовки запроса. Для Wan соотношение сторон берётся из первого кадра, ratio не передаётся."""
    spec = MODELS[model]
    if model == 'wan':
        media = [{'type': 'first_frame', 'url': first}] + ([{'type': 'last_frame', 'url': last}] if last else [])
        body = {'model': spec['id'], 'input': {'prompt': prompt, 'media': media},
                'parameters': {'resolution': resolution.upper(), 'duration': duration,
                               'prompt_extend': False, 'watermark': False}}
        if negative:
            body['input']['negative_prompt'] = negative
        if seed is not None:
            body['parameters']['seed'] = seed
        return body, {'X-DashScope-Async': 'enable'}
    text = ('The first frame is image 1 and the final frame is image 2. ' if last else '') + prompt
    if negative:
        text += ' Avoid: ' + negative + '.'
    content = [{'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': first}, 'role': 'first_frame'}]
    if last:
        content.append({'type': 'image_url', 'image_url': {'url': last}, 'role': 'last_frame'})
    body = {'model': spec['id'], 'content': content, 'ratio': ratio or 'adaptive', 'duration': duration,
            'resolution': resolution, 'watermark': False, 'generate_audio': bool(audio)}
    if seed is not None:
        body['seed'] = seed
    return body, {}


def read_key(name: str) -> str | None:
    if os.getenv(name):
        return os.environ[name]
    if CREDENTIALS.is_file():
        for line in CREDENTIALS.read_text().splitlines():
            match = re.match(rf'^{name}=(.*)$', line.strip())
            if match:
                return match.group(1).strip().strip('"\'')
    return None


def http(method, url, key=None, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {'Accept': 'application/json', **(headers or {})}
    if key:
        hdrs['Authorization'] = 'Bearer ' + key
    if data is not None:
        hdrs['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode()) if raw else {}


def reachable(url: str) -> bool:
    # Cloudflare перед публичными сайтами часто отвечает 403 на стандартный User-Agent Python-urllib: нужен свой заголовок.
    req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0 (compatible; video_generate/1.0)'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def publish_frame(src: str) -> tuple[str, Path | None]:
    """https-ссылку отдаёт как есть; локальный файл кладёт в PUBLIC_DIR под случайным именем."""
    if re.match(r'^https://', src):
        return src, None
    path = Path(src)
    if not path.is_file() or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        sys.exit(f'Кадр {src}: нужен существующий PNG, JPEG или WebP либо https-ссылка')
    if PUBLIC_DIR is None or not PUBLIC_URL.startswith('https://'):
        sys.exit('Локальный кадр модель не скачает. Передайте https-ссылку или настройте IMAGE_GEN_PUBLIC_DIR и '
                 'IMAGE_GEN_PUBLIC_URL (своя публичная папка на вашем сервере), см. references/video.md.')
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    target = PUBLIC_DIR / (secrets.token_hex(12) + path.suffix.lower())
    shutil.copyfile(path, target)
    target.chmod(0o644)
    return PUBLIC_URL.rstrip('/') + '/' + target.name, target


def cleanup(paths) -> None:
    for p in paths:
        Path(p).unlink(missing_ok=True)


def ledger(entry: dict) -> None:
    state().mkdir(parents=True, exist_ok=True)
    entry['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(state('ledger.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def find(data, keys):
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), str) and data[k]:
                return data[k]
        data = list(data.values())
    if isinstance(data, list):
        for value in data:
            found = find(value, keys)
            if found:
                return found
    return None


def out_dir(name: str, project: str | None) -> Path:
    return STATE_DIR / 'creatives' / project / 'videos' / name if project else state('videos', name)


def write_job(name: str, job: dict) -> None:
    job_path(name).parent.mkdir(parents=True, exist_ok=True)
    job_path(name).write_text(json.dumps(job, ensure_ascii=False, indent=1))


def create(a) -> int:
    spec = MODELS[a.model]
    if not NAME.match(a.name):
        sys.exit('--name: латиница в нижнем регистре, цифры, точка, дефис, подчёркивание')
    if job_path(a.name).exists():
        sys.exit(f'Задача {a.name} уже была ({job_path(a.name)}). Повторно не отправляю: новая генерация = новое имя '
                 'и явное согласие пользователя на оплату.')
    if a.duration not in spec['durations'] or a.resolution not in spec['resolutions']:
        sys.exit(f'{a.model}: длительность {spec["durations"].start}–{spec["durations"].stop - 1} с, '
                 f'разрешение {", ".join(spec["resolutions"])}')
    cost = estimate_usd(a.model, a.duration, a.resolution)
    print(f'{a.model} ({spec["id"]}), {a.duration} с, {a.resolution}: примерно ${cost}')
    if a.dry_run:
        body, _ = build_request(a.model, a.first, a.last, a.prompt, a.negative, a.duration, a.resolution,
                                a.ratio, a.seed, a.audio)
        print(json.dumps(body, ensure_ascii=False, indent=1))
        return 0
    key = read_key(spec['key'])
    if not key:
        sys.exit(f'Нет {spec["key"]}: токен группы {spec["group"]} с оплатой по объёму создаётся в '
                 'api2.laozhang.ai/token, ключ дописывается в ~/.config/media-skills/image-gen.env')
    temp, urls = [], []
    for src in filter(None, (a.first, a.last)):
        url, path = publish_frame(src)
        urls.append(url)
        if path:
            temp.append(str(path))
    for url in urls:
        if not reachable(url):
            cleanup(temp)
            sys.exit(f'Кадр не открывается по ссылке {url}; модель его не скачает, запрос не отправлен')
    body, headers = build_request(a.model, urls[0], urls[1] if a.last else None, a.prompt, a.negative,
                                  a.duration, a.resolution, a.ratio, a.seed, a.audio)
    job = {'name': a.name, 'model': a.model, 'model_id': spec['id'], 'project': a.project,
           'out_dir': str(out_dir(a.name, a.project)), 'estimate_usd': cost, 'temp': temp,
           'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'request': body}
    ledger({'event': 'started', 'name': a.name, 'model': a.model, 'estimate_usd': cost, 'body': body})
    try:
        resp = http('POST', BASE_URL + spec['create'], key, body, headers)
    except HTTPError as e:
        text = e.read().decode(errors='replace')[:1500]
        ledger({'event': 'http_error', 'name': a.name, 'status': e.code, 'body': text})
        cleanup(temp)
        hint = ''
        if 'pay-per-request' in text:
            hint = (f'\nТокен группы {spec["group"]} стоит на оплате за запрос. Нужно в api2.laozhang.ai/token '
                    'переключить его на оплату по объёму (pay-as-you-go), ключ не меняется. Ничего не списано.')
        sys.exit(f'HTTP {e.code}: {text}{hint}')
    except (URLError, TimeoutError, OSError) as e:
        write_job(a.name, {**job, 'status': 'unknown_after_send', 'error': str(e)})
        ledger({'event': 'unknown_after_send', 'name': a.name, 'error': str(e)})
        print(f'Ответа нет ({e}). Задача могла создаться и списаться: повторно НЕ отправлять, проверить '
              'журнал api2.laozhang.ai/log. Временные кадры оставлены, пока задача может их скачивать.')
        return 2
    task_id = find(resp, ('task_id', 'id'))
    write_job(a.name, {**job, 'status': 'created' if task_id else 'unknown_after_send', 'task_id': task_id,
                       'response': resp})
    ledger({'event': 'created', 'name': a.name, 'task_id': task_id, 'response': resp})
    if not task_id:
        print(f'Ответ без id задачи: {json.dumps(resp, ensure_ascii=False)[:500]}. Повторно не отправлять.')
        return 2
    print(f'Задача {task_id}. Дальше: video_generate.py poll --name {a.name}')
    return 0


def poll(a) -> int:
    job = json.loads(job_path(a.name).read_text())
    if job.get('status') in ('completed', 'failed'):
        print(json.dumps({k: job.get(k) for k in ('status', 'output', 'usd', 'error')}, ensure_ascii=False))
        return 0 if job['status'] == 'completed' else 1
    if not job.get('task_id'):
        sys.exit('У задачи нет id: смотреть журнал api2.laozhang.ai/log, повторно не отправлять')
    spec = MODELS[job['model']]
    key = read_key(spec['key'])
    deadline = time.time() + a.timeout * 60
    while time.time() < deadline:
        try:
            data = http('GET', BASE_URL + spec['poll'].format(id=job['task_id']), key)
        except (HTTPError, URLError, TimeoutError, OSError) as e:
            print('опрос не удался:', e, flush=True)
            time.sleep(15)
            continue
        status = str(find(data, ('status', 'task_status')) or '').lower()
        print(time.strftime('%H:%M:%S'), status, find(data, ('progress',)) or '', flush=True)
        target = Path(job['out_dir'])
        if status in ('succeeded', 'completed', 'success', 'succeed'):
            target.mkdir(parents=True, exist_ok=True)
            (target / 'result.json').write_text(json.dumps(data, ensure_ascii=False, indent=1))
            with urllib.request.urlopen(find(data, ('video_url', 'result_url', 'url')), timeout=180) as r:
                (target / 'raw.mp4').write_bytes(r.read())
            tokens = (data.get('usage') or {}).get('completion_tokens')
            usd = round(tokens / 1e6 * SEEDANCE_USD_PER_MTOK, 2) if tokens else job['estimate_usd']
            cleanup(job.get('temp', []))
            job.update(status='completed', output=str(target / 'raw.mp4'), usd=usd, temp=[])
            write_job(a.name, job)
            ledger({'event': 'completed', 'name': a.name, 'task_id': job['task_id'], 'usd': usd})
            print(f'Готово: {target / "raw.mp4"} (около ${usd})')
            return 0
        if status in ('failed', 'fail', 'expired', 'cancelled', 'canceled'):
            cleanup(job.get('temp', []))
            job.update(status='failed', error=json.dumps(data, ensure_ascii=False)[:1500], temp=[])
            write_job(a.name, job)
            ledger({'event': 'failed', 'name': a.name, 'task_id': job['task_id'], 'data': data})
            print('Задача упала:', job['error'])
            return 1
        time.sleep(10)
    print(f'Не дождался за {a.timeout} мин; задача ещё может завершиться, повторить poll позже')
    return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Видео из картинки: Wan 2.7 i2v / Seedance 2.0 через laozhang')
    sub = parser.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('create', help='одна платная генерация')
    c.add_argument('--model', default='seedance', choices=MODELS,
                   help='seedance всегда по умолчанию; wan только по явной просьбе')
    c.add_argument('--name', required=True, help='уникальное имя задачи, например acme-coin-seedance')
    c.add_argument('--first', required=True, help='первый кадр: файл или https-ссылка')
    c.add_argument('--last', help='последний кадр: файл или https-ссылка')
    c.add_argument('--prompt', required=True)
    c.add_argument('--negative', help='что исключить (Wan: negative_prompt, Seedance: дописывается в текст)')
    c.add_argument('--duration', type=int, default=5)
    c.add_argument('--resolution', default='720p', choices=('480p', '720p', '1080p'))
    c.add_argument('--ratio', help='только Seedance: adaptive (по первому кадру), 1:1, 16:9, 9:16, 4:3, 3:4, 21:9')
    c.add_argument('--seed', type=int)
    c.add_argument('--audio', action='store_true', help='Seedance: генерировать звук (по умолчанию нет)')
    c.add_argument('--project', help='клиент/дата-слаг: raw.mp4 ляжет в <data>/creatives/<project>/videos/<name>')
    c.add_argument('--dry-run', action='store_true', help='показать тело запроса и цену, ничего не отправлять')
    p = sub.add_parser('poll', help='дождаться и скачать')
    p.add_argument('--name', required=True)
    p.add_argument('--timeout', type=int, default=25, help='минут')
    s = sub.add_parser('status', help='показать job-файл')
    s.add_argument('--name', required=True)
    a = parser.parse_args(argv)
    if a.cmd == 'create':
        return create(a)
    if a.cmd == 'poll':
        return poll(a)
    print(job_path(a.name).read_text())
    return 0


if __name__ == '__main__':
    sys.exit(main())
