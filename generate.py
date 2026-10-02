#!/usr/bin/env python3
"""
image-gen — генератор одиночных изображений через laozhang.ai
Дефолт — GPT Image 2.5 (с 2026-09-10); gpt-image-2 только по явному --model.
Nano Banana / Gemini (Flash и Pro) НЕ использовать никогда.
Поддерживает референсные изображения через --ref.
Принимает готовый промт → сжимает и архивирует → печатает путь готового файла строкой __FILE__:<путь>

Защита: file-lock (один процесс), ledger (каждый запрос логируется), zero-retry на таймаут.
"""

import os
import re
import sys
import base64
import argparse
import time
import shutil
import fcntl
from pathlib import Path
from dotenv import dotenv_values
import requests

from ledger import log_started, log_success, log_failed, log_prompt_md
from compress import archive_dir, save_compressed
from skill_config import ENV_FILE, CACHE_DIR, private_dir

# Ключ: окружение → канонический файл кредов. Раньше скрипт искал только
# `.agents/config/.env`, которого нет, и каждый вызывающий подставлял ключ сам.
KEY_FILES = [ENV_FILE]


def load_api_key() -> str | None:
    key = os.getenv("LAOZHANG_API_KEY")
    for path in KEY_FILES:
        if key:
            break
        if path.is_file():
            key = dotenv_values(path).get("LAOZHANG_API_KEY")
    return key


API_KEY = load_api_key()
# api2 — базовый URL из актуальной документации laozhang (docs.laozhang.ai, 09.2026);
# api.laozhang.ai отдаёт тот же каталог моделей, но лежит за одним IP против пяти у api2.
API_BASE = "https://api2.laozhang.ai/v1"
API_URL_GENERATE = f"{API_BASE}/images/generations"
API_URL_EDIT = f"{API_BASE}/images/edits"
OUTPUT_DIR = private_dir(CACHE_DIR / "outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REFS_DIR = private_dir(CACHE_DIR / "refs")
REFS_DIR.mkdir(parents=True, exist_ok=True)
LOCK_FILE = CACHE_DIR / "generation.lock"

COST = 0.03  # все линии ниже — $0.03/запрос, группа default у laozhang

# Алиас в --model → имя модели в запросе. Проверено запросами 10.09.2026:
#   gpt-image-2.5-web      — веб-линия 2.5 (реверс ChatGPT), единственная живая 2.5
#                            у нашего токена; size точный, edits с рефами работают,
#                            quality принимает, но usage от него не меняется.
#   gpt-image-2.5-*-vip    — официальные линии по $0.03 с quality low..max и
#                            background=transparent; 10.09 отвечали 503
#                            model_service_unavailable (нет каналов). Алиасы
#                            оставлены, чтобы включить одной командой, когда поднимут.
#   gpt-image-2-vip        — прежняя модель; обычный gpt-image-2 в группе default
#                            не принимает size, поэтому старое имя тоже уходит как vip.
#   02.10.2026: веб-линия отвечала HTTP 500 do_request_failed на всё подряд, а flare-vip
#   ожила и рисует так же за $0.03. Такой ответ laozhang не тарифицирует (в журнале
#   потребления этих запросов нет), поэтому generate_image сам повторяет его один раз на
#   линии из WEB_FALLBACK.
MODELS = {
    "gpt-image-2.5": "gpt-image-2.5-web",
    "gpt-image-2.5-flare": "gpt-image-2.5-flare-vip",
    "gpt-image-2.5-sunburst": "gpt-image-2.5-sunburst-vip",
    "gpt-image-2": "gpt-image-2-vip",
}
DEFAULT_MODEL = "gpt-image-2.5"
WEB_FALLBACK = {"gpt-image-2.5-web": "gpt-image-2.5-flare-vip"}
QUALITIES = ("low", "medium", "high", "xhigh", "max")


def normalize_model(model: str) -> str:
    model = (model or DEFAULT_MODEL).strip()
    if model in MODELS:
        return MODELS[model]
    if model in MODELS.values():
        return model
    raise SystemExit(f"Модель {model} не поддерживается. Доступно: {', '.join(MODELS)} "
                     f"(дефолт {DEFAULT_MODEL}; gpt-image-2 только по явному указанию)")


# Размер запроса к провайдеру: генерируем в 2K, потому что цена поштучная и от
# размера не зависит, а качество зависит. Все стороны кратны 16 (требование API).
GPT_IMAGE_SIZES = {
    "1:1": "2048x2048",
    "3:2": "2048x1360",
    "2:3": "1360x2048",
    "16:9": "2048x1152",
    "9:16": "1152x2048",
    "4:3": "2048x1536",
    "3:4": "1536x2048",
    "4:5": "1632x2048",
    "5:4": "2048x1632",
    "21:9": "2048x864",
}

# Размер файла на диске. Генерируем в 2K, а кладём то, чем реально пользуются:
# сторис 1080x1920, квадрат под пост 1080x1080, лендскейп 1920x1080. Решение
# 06.08.2026: крупнее размера доставки файлы не нужны. На деньги это не влияет вовсе
# (оплата поштучная), только на вес файла. Оригинал 2K сохраняется флагом
# --keep-2k: он нужен там, где картинку потом кладут на холст крупнее 1080,
# например обложки carousel-generator на 2160px.
DELIVERY_SIZES = {
    "1:1": (1080, 1080),
    "3:2": (1620, 1080),
    "2:3": (1080, 1620),
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
    "4:5": (1080, 1350),
    "5:4": (1350, 1080),
    "21:9": (1920, 810),
}

# 5 минут: по ledger за июль–сентябрь максимум успешной генерации 232 сек,
# а пять запросов упали как unknown_billed ровно на прежних 240.
REQUEST_TIMEOUT = 300


def validate_size(size: str) -> str:
    """Правила API (одинаковые у web и vip линий, текст ошибки 400 от 10.09.2026):
    стороны кратны 16, не больше 3840, соотношение от 1:3 до 3:1, пикселей
    от 655 360 до 8 294 400. Проверяем до отправки: 400 приходит мгновенно и
    бесплатно, но понятнее сказать сразу, что 1080x1920 надо писать как 1088x1920."""
    w, h = (int(v) for v in size.split("x"))
    problems = []
    if w % 16 or h % 16:
        problems.append("стороны должны быть кратны 16")
    if max(w, h) > 3840:
        problems.append("сторона не больше 3840")
    if max(w, h) / min(w, h) > 3:
        problems.append("соотношение от 1:3 до 3:1")
    if not 655_360 <= w * h <= 8_294_400:
        problems.append("пикселей от 655360 до 8294400")
    if problems:
        raise SystemExit(f"--size {size} не примет API: " + "; ".join(problems))
    return size


def resolve_gpt_size(size: str, aspect_ratio: str) -> str:
    """Явный --size вида 2160x3840 бьёт таблицу (4K принимается). Иначе — размер по ratio."""
    if size and re.fullmatch(r"\d+x\d+", size):
        return validate_size(size)
    return GPT_IMAGE_SIZES.get(aspect_ratio, "2048x2048")


def get_cost(model: str = "") -> float:
    return COST


def extract_image_bytes(data: dict) -> bytes:
    """Извлекает байты изображения из ответа API (b64 или url)."""
    item = data["data"][0]
    if "b64_json" in item and item["b64_json"]:
        value = item["b64_json"]
        if value.startswith("data:"):
            value = value.split(",", 1)[1]
        return base64.b64decode(value)
    elif "url" in item and item["url"]:
        img_resp = requests.get(item["url"], timeout=60)
        img_resp.raise_for_status()
        return img_resp.content
    else:
        raise RuntimeError(f"Нет данных изображения в ответе: {list(item.keys())}")


def error_text(resp: requests.Response) -> str:
    """HTTP-код плюс тело ошибки провайдера: без него 400/451 в ledger были немыми."""
    try:
        err = resp.json().get("error", {})
        detail = err.get("message") or resp.text
        code = err.get("code")
        return f"HTTP {resp.status_code}" + (f" {code}" if code else "") + f": {detail}"
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"


def generate_image(
    prompt: str,
    output_path: Path,
    model: str = DEFAULT_MODEL,
    size: str = "2K",
    aspect_ratio: str = "1:1",
    ref_images: list[str] | None = None,
    job_id: str | None = None,
    keep_2k: bool = False,
    quality: str | None = None,
    project: str | None = None,
) -> Path:
    """
    Вызывает API, сжимает и сохраняет файл в постоянный архив.
    Retry только на 429/503 (запрос гарантированно не начат).
    4xx — отказ провайдера до генерации (валидация, модерация 451): failed, не тарифицирован.
    Таймаут и connection reset после отправки → unknown_billed, без retry.
    """
    if not API_KEY:
        raise RuntimeError(f"LAOZHANG_API_KEY не найден ни в окружении, ни в {', '.join(map(str, KEY_FILES))}")

    archive_dir(project)  # validate local destination before any paid request
    requested_model = model
    model = normalize_model(model)
    if quality and quality not in QUALITIES:
        raise SystemExit(f"--quality {quality}: допустимо {', '.join(QUALITIES)}")
    cost = get_cost(model)
    if not job_id:
        job_id = f"single_{int(time.time())}"

    gpt_size = resolve_gpt_size(size, aspect_ratio)
    use_edit = bool(ref_images)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    max_safe_retries = 2  # только для 429/503
    ledger_fields = dict(size=gpt_size, quality=quality, refs=ref_images or [], project=project,
                         requested_model=requested_model)
    md = dict(name=output_path.stem, prompt=prompt, model=model, size=gpt_size, ratio=aspect_ratio,
              quality=quality, refs=ref_images, project=project)
    t0 = time.time()

    fell_back = False
    for attempt in range(1, max_safe_retries + 3):  # +1 попытка только под переход с веб-линии
        # Логируем ПЕРЕД отправкой
        log_started(job_id, prompt, model, aspect_ratio, output_path.stem, cost, **ledger_fields)

        request_sent = False
        files = []
        try:
            params = {"model": model, "prompt": prompt, "size": gpt_size}
            if quality:
                params["quality"] = quality
            if use_edit:
                for ref_path in ref_images:
                    ref_mime = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(Path(ref_path).suffix.lower(), "image/png")
                    files.append(("image[]", (Path(ref_path).name, open(ref_path, "rb"), ref_mime)))
                request_sent = True
                resp = requests.post(
                    API_URL_EDIT, headers=headers, data=params, files=files, timeout=REQUEST_TIMEOUT,
                )
            else:
                headers["Content-Type"] = "application/json"
                request_sent = True
                resp = requests.post(API_URL_GENERATE, headers=headers, json=params, timeout=REQUEST_TIMEOUT)

            # 429/503 — запрос не начат, safe retry
            if resp.status_code in (429, 503):
                log_failed(job_id, error_text(resp), billed=False)
                if attempt <= max_safe_retries:
                    delay = [5, 15][min(attempt - 1, 1)]
                    print(f"  [{resp.status_code}] retry {attempt}/{max_safe_retries} через {delay} сек...")
                    time.sleep(delay)
                    job_id = f"{job_id}_r{attempt}"  # новый job_id для retry
                    continue
                raise RuntimeError(f"{error_text(resp)} — после {max_safe_retries} попыток")

            # Провайдер не достучался до линии (do_request_failed): ответ пришёл, не тарифицирован.
            if resp.status_code >= 500 and "do_request_failed" in resp.text:
                err = error_text(resp)
                log_failed(job_id, err, billed=False)
                log_prompt_md(**md, status="failed", error=err)
                if model in WEB_FALLBACK and not fell_back:
                    fell_back = True
                    model = WEB_FALLBACK[model]
                    md["model"] = model
                    job_id = f"{job_id}_fb"
                    print(f"  [500 do_request_failed] линия лежит, повторяю на {model} (не тарифицировано)")
                    continue
                raise RuntimeError(f"{err} (линия недоступна, не тарифицировано)")

            if resp.status_code >= 400:
                err = error_text(resp)
                # 4xx провайдер отвергает до генерации (size, модерация 451) — не тарифицируется.
                # 5xx после отправки считаем неизвестно-оплаченным, как таймаут.
                log_failed(job_id, err, billed=resp.status_code >= 500)
                log_prompt_md(**md, status="failed" if resp.status_code < 500 else "unknown_billed", error=err)
                raise RuntimeError(err)

            img_bytes = extract_image_bytes(resp.json())
            output_path = save_compressed(
                img_bytes, output_path.name, project,
                target=None if keep_2k else DELIVERY_SIZES.get(aspect_ratio),
            )
            md_path = log_prompt_md(**md, status="success", output=str(output_path), elapsed=time.time() - t0)
            print(f"  промпт записан: {md_path}")
            return output_path

        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            if request_sent:
                # Запрос ушёл → сервер мог начать генерацию → тарифицирован
                log_failed(job_id, str(e), billed=True)
                log_prompt_md(**md, status="unknown_billed", error=str(e))
                print(f"  [ТАЙМАУТ/DISCONNECT] {type(e).__name__}")
                print(f"  ⚠️ Запрос мог быть тарифицирован. Ретрай НЕ делаем.")
                raise
            else:
                # Запрос не ушёл — safe
                log_failed(job_id, str(e), billed=False)
                raise
        except requests.RequestException as e:
            log_failed(job_id, str(e), billed=request_sent)
            raise
        finally:
            for _, (_, fobj, _) in files:
                fobj.close()

    raise RuntimeError(f"Не удалось сгенерировать: {output_path.name}")


def main():
    parser = argparse.ArgumentParser(description="image-gen — генератор одиночных изображений")
    parser.add_argument("--prompt", required=True, help="Промт для генерации")
    parser.add_argument("--ratio", default="1:1", help="Aspect ratio (default: 1:1)")
    parser.add_argument("--size", default="2K", help="Обычно не нужен: размер берётся из --ratio. "
                                                     "Явный ШxВ (например 2160x3840) уходит в API как есть; стороны кратны 16")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"{', '.join(MODELS)} (default: {DEFAULT_MODEL}; gpt-image-2 только по явному указанию)")
    parser.add_argument("--quality", default=None, choices=QUALITIES,
                        help="low/medium/high/xhigh/max; без флага не передаётся")
    parser.add_argument("--keep-2k", action="store_true",
                        help="не уменьшать до размера доставки, оставить оригинал 2K "
                             "(нужно, если картинка ложится на холст шире 1080)")
    parser.add_argument("--name", default="", help="Имя файла без расширения")
    parser.add_argument("--ref", nargs="+", default=None, help="Референсные изображения (image-to-image через /images/edits)")
    parser.add_argument("--project", default=None,
                        help="Проект/клиент, например acme/2026-09-16-launch: промпт запишется в "
                             "<IMAGE_GEN_DATA_DIR>/creatives/<project>/prompts.md")

    args = parser.parse_args()

    model = normalize_model(args.model)

    # Валидация референсов
    if args.ref:
        stable_refs = []
        for ref_path in args.ref:
            p = Path(ref_path)
            if not p.exists():
                raise FileNotFoundError(f"Референс не найден: {ref_path}")
            if str(p.parent) == str(OUTPUT_DIR):
                ref_copy = REFS_DIR / p.name
                shutil.copy2(p, ref_copy)
                stable_refs.append(str(ref_copy))
                print(f"💾 Референс сохранён: {ref_copy}")
            else:
                stable_refs.append(ref_path)
        args.ref = stable_refs

    filename = f"{args.name}.png" if args.name else f"image_gen_{int(time.time())}.png"
    output_path = OUTPUT_DIR / filename
    cost = get_cost(model)
    job_id = f"single_{int(time.time())}_{args.name or 'img'}"

    gpt_size = resolve_gpt_size(args.size, args.ratio)
    print(f"Модель: {args.model} → {model}")
    print(f"Размер: {gpt_size} (ratio: {args.ratio})" + (f", quality: {args.quality}" if args.quality else ""))

    if args.ref:
        print(f"Референсы: {len(args.ref)} изображений")
        for r in args.ref:
            print(f"  - {r}")

    print(f"Стоимость: ${cost}")
    print(f"Генерирую...")

    t0 = time.time()
    output_path = generate_image(
        prompt=args.prompt,
        output_path=output_path,
        model=args.model,
        size=args.size,
        aspect_ratio=args.ratio,
        ref_images=args.ref,
        job_id=job_id,
        keep_2k=args.keep_2k,
        quality=args.quality,
        project=args.project,
    )
    elapsed = time.time() - t0

    log_success(job_id, str(output_path), elapsed)

    ref_copy = REFS_DIR / output_path.name
    shutil.copy2(output_path, ref_copy)

    print(f"✓ Готово: {output_path.name} ({elapsed:.0f} сек)")
    print(f"💾 Копия: {ref_copy}")
    delivery_path = OUTPUT_DIR / output_path.name
    shutil.copy2(output_path, delivery_path)
    print(f"__FILE__:{delivery_path}")


if __name__ == "__main__":
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⛔ Другая генерация уже запущена. Жди завершения.")
        print("   Параллель — только через batch_generate.py (один процесс, lock, ledger).")
        sys.exit(1)
    try:
        main()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
