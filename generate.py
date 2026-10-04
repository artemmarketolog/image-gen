#!/usr/bin/env python3
"""
image-gen — генератор одиночных изображений.
Провайдеры: laozhang.ai (по умолчанию, $0.03 за картинку) и официальный OpenAI
(--provider openai, оплата по токенам). Когда у laozhang лежат все линии 2.5,
запрос сам уходит в OpenAI на ту же модель 2.5 (с 03.10.2026).
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

from ledger import log_started, log_success, log_failed, log_billed, log_prompt_md
from compress import archive_dir, save_compressed
from skill_config import ENV_FILE, CACHE_DIR, private_dir

# Ключ: окружение → канонический файл кредов. Раньше скрипт искал только
# `.agents/config/.env`, которого нет, и каждый вызывающий подставлял ключ сам.
KEY_FILES = [ENV_FILE]


OPENAI_KEY_FILES = [ENV_FILE]


def load_api_key(name: str = "LAOZHANG_API_KEY", files: list[Path] = KEY_FILES) -> str | None:
    key = os.getenv(name)
    for path in files:
        if key:
            break
        if path.is_file():
            key = dotenv_values(path).get(name)
    return key


API_KEY = load_api_key()
OPENAI_API_KEY = load_api_key("OPENAI_API_KEY", OPENAI_KEY_FILES)
OPENAI_API_BASE = "https://api.openai.com/v1"
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

# Официальный OpenAI: те же алиасы → настоящие имена моделей. Flare — быстрая
# повседневная, Sunburst — точнее в правках по референсу; цена за токен одинаковая.
PROVIDERS = ("laozhang", "openai")
DEFAULT_PROVIDER = "laozhang"
OPENAI_MODELS = {
    "gpt-image-2.5": "gpt-image-2.5-flare",
    "gpt-image-2.5-flare": "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst": "gpt-image-2.5-sunburst",
    "gpt-image-2": "gpt-image-2",
}
# Линия laozhang → модель OpenAI при автопереходе, когда у laozhang лежит 2.5.
# gpt-image-2-vip сюда не входит: старую модель берём только по явной просьбе.
LAOZHANG_TO_OPENAI = {
    "gpt-image-2.5-web": "gpt-image-2.5-flare",
    "gpt-image-2.5-flare-vip": "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst-vip": "gpt-image-2.5-sunburst",
}
# Без --quality OpenAI сам выбирает уровень (auto) и цена плавает, поэтому шлём medium.
# Решение 03.10.2026: medium по умолчанию, high только руками (живые люди, лица).
OPENAI_DEFAULT_QUALITY = "medium"
# $ за 1M токенов (docs/models/gpt-image-2.5-flare, у sunburst и gpt-image-2 те же).
OPENAI_RATES = {"text_in": 5.0, "image_in": 8.0, "image_out": 30.0}
# Оценка до отправки, $ за картинку 1152x2048 (9:16). Замерено 03.10.2026, flare:
# low 157 токенов, medium 367, high 1413, xhigh 2511, max 5650; sunburst high те же 1413.
# Площадь дорожает быстрее линейного: 2048x2048 high — 3568 токенов ($0.107, в 2.5 раза
# дороже при площади в 1.8 раза), отсюда степень 1.5. Меньше 9:16 2K цена почти не падает
# (1024x1024 high — 1756 токенов), поэтому снизу не масштабируем.
# Каждый --ref добавляет около 1500 входных image-токенов (≈$0.012).
OPENAI_ESTIMATE = {"low": 0.005, "medium": 0.011, "high": 0.043, "xhigh": 0.076, "max": 0.17}
OPENAI_ESTIMATE_PIXELS = 1152 * 2048
OPENAI_REF_ESTIMATE = 0.012


class NotBilledError(RuntimeError):
    """Провайдер ответил отказом до генерации: деньги не списаны, повтор безопасен."""


def normalize_model(model: str, provider: str = DEFAULT_PROVIDER) -> str:
    model = (model or DEFAULT_MODEL).strip()
    table = OPENAI_MODELS if provider == "openai" else MODELS
    if model in table:
        return table[model]
    if model in table.values():
        return model
    raise SystemExit(f"Модель {model} не поддерживается у {provider}. Доступно: {', '.join(table)} "
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


# У OpenAI цена растёт с площадью: квадрат 2048 на high — $0.107, 1024 — вдвое дешевле.
# Решение 03.10.2026: квадрат в OpenAI — 1024x1024 (явный --size по-прежнему бьёт).
OPENAI_SIZES = {"1:1": "1024x1024"}


def resolve_gpt_size(size: str, aspect_ratio: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Явный --size вида 2160x3840 бьёт таблицу (4K принимается). Иначе — размер по ratio."""
    if size and re.fullmatch(r"\d+x\d+", size):
        return validate_size(size)
    if provider == "openai" and aspect_ratio in OPENAI_SIZES:
        return OPENAI_SIZES[aspect_ratio]
    return GPT_IMAGE_SIZES.get(aspect_ratio, "2048x2048")


def get_cost(model: str = "", provider: str = DEFAULT_PROVIDER, quality: str | None = None,
             refs: int = 0, size: str | None = None) -> float:
    """Цена до отправки: у laozhang точная, у OpenAI оценка по замерам (факт считает openai_cost)."""
    if provider != "openai":
        return COST
    scale = 1.0
    if size and re.fullmatch(r"\d+x\d+", size):
        w, h = (int(v) for v in size.split("x"))
        scale = max(1.0, w * h / OPENAI_ESTIMATE_PIXELS) ** 1.5
    return OPENAI_ESTIMATE[quality or OPENAI_DEFAULT_QUALITY] * scale + refs * OPENAI_REF_ESTIMATE


def openai_cost(usage: dict) -> float:
    """Фактическая цена запроса OpenAI по usage из ответа."""
    details = usage.get("input_tokens_details")
    text_in = details.get("text_tokens", 0) if details else usage.get("input_tokens", 0)
    image_in = details.get("image_tokens", 0) if details else 0
    out = usage.get("output_tokens", 0)
    return (text_in * OPENAI_RATES["text_in"] + image_in * OPENAI_RATES["image_in"]
            + out * OPENAI_RATES["image_out"]) / 1_000_000


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
        text = f"HTTP {resp.status_code}" + (f" {code}" if code else "") + f": {detail}"
        # OpenAI при блокировке модерацией говорит, на каком шаге и за что
        if err.get("moderation_details"):
            text += f" (moderation: {err['moderation_details']})"
        return text
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"


def error_code(resp: requests.Response) -> str:
    try:
        return resp.json().get("error", {}).get("code") or ""
    except ValueError:
        return ""


# Фактическая цена по job_id, который передал вызывающий (batch берёт её в отчёт).
ACTUAL_COSTS: dict[str, float] = {}


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
    provider: str = DEFAULT_PROVIDER,
    background: str | None = None,
    moderation: str | None = None,
) -> Path:
    """
    Вызывает API, сжимает и сохраняет файл в постоянный архив.
    Retry только когда запрос гарантированно не начат: 429/503 у laozhang, 429 и 5xx у OpenAI.
    Когда у laozhang лежат все линии 2.5, запрос один раз уходит в OpenAI на ту же модель.
    4xx — отказ провайдера до генерации (валидация, модерация): NotBilledError.
    Таймаут и connection reset после отправки → unknown_billed, без retry.
    """
    if provider not in PROVIDERS:
        raise SystemExit(f"--provider {provider}: допустимо {', '.join(PROVIDERS)}")
    archive_dir(project)  # validate local destination before any paid request
    requested_model = model
    model = normalize_model(model, provider)
    if quality and quality not in QUALITIES:
        raise SystemExit(f"--quality {quality}: допустимо {', '.join(QUALITIES)}")
    if provider == "openai":
        if not OPENAI_API_KEY:
            raise RuntimeError(f"OPENAI_API_KEY не найден ни в окружении, ни в {OPENAI_KEY_FILES[0]}")
        quality = quality or OPENAI_DEFAULT_QUALITY
    elif not API_KEY:
        raise RuntimeError(f"LAOZHANG_API_KEY не найден ни в окружении, ни в {', '.join(map(str, KEY_FILES))}")
    if not job_id:
        job_id = f"single_{int(time.time())}"
    caller_job_id = job_id

    gpt_size = resolve_gpt_size(size, aspect_ratio, provider)
    use_edit = bool(ref_images)
    max_safe_retries = 2
    t0 = time.time()
    retries = 0
    fell_back = False

    def go_openai(reason: str) -> bool:
        """Линия laozhang лежит → та же 2.5 в OpenAI. False, если переходить некуда."""
        nonlocal provider, model, quality, job_id, retries, gpt_size
        if provider != "laozhang" or model not in LAOZHANG_TO_OPENAI or not OPENAI_API_KEY:
            return False
        provider, model = "openai", LAOZHANG_TO_OPENAI[model]
        gpt_size = resolve_gpt_size(size, aspect_ratio, provider)
        quality = quality or OPENAI_DEFAULT_QUALITY
        job_id, retries = f"{job_id}_oa", 0
        print(f"  [{reason}] у laozhang 2.5 недоступна, перехожу в OpenAI: {model}, quality {quality} "
              f"(≈${get_cost(model, provider, quality, len(ref_images or []), gpt_size):.3f})")
        return True

    while True:
        cost = get_cost(model, provider, quality, len(ref_images or []), gpt_size)
        md = dict(name=output_path.stem, prompt=prompt, model=model, size=gpt_size, ratio=aspect_ratio,
                  quality=quality, refs=ref_images, project=project, provider=provider)
        # Логируем ПЕРЕД отправкой
        log_started(job_id, prompt, model, aspect_ratio, output_path.stem, cost, size=gpt_size,
                    quality=quality, refs=ref_images or [], project=project,
                    requested_model=requested_model, provider=provider)

        base = OPENAI_API_BASE if provider == "openai" else API_BASE
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY if provider == 'openai' else API_KEY}"}
        request_sent = False
        files = []
        try:
            params = {"model": model, "prompt": prompt, "size": gpt_size}
            if quality:
                params["quality"] = quality
            if background:
                params["background"] = background
                if background == "transparent":
                    params["output_format"] = "png"
            if moderation and provider == "openai":
                params["moderation"] = moderation
            if use_edit:
                for ref_path in ref_images:
                    ref_mime = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(Path(ref_path).suffix.lower(), "image/png")
                    files.append(("image[]", (Path(ref_path).name, open(ref_path, "rb"), ref_mime)))
                request_sent = True
                resp = requests.post(
                    f"{base}/images/edits", headers=headers, data=params, files=files, timeout=REQUEST_TIMEOUT,
                )
            else:
                headers["Content-Type"] = "application/json"
                request_sent = True
                resp = requests.post(f"{base}/images/generations", headers=headers, json=params,
                                     timeout=REQUEST_TIMEOUT)

            code = error_code(resp) if resp.status_code >= 400 else ""
            # Деньги на балансе OpenAI кончились: повтор не поможет
            if provider == "openai" and code == "insufficient_quota":
                err = error_text(resp)
                log_failed(job_id, err, billed=False)
                log_prompt_md(**md, status="failed", error=err)
                raise NotBilledError(f"{err} — пополнить баланс на platform.openai.com")

            # Запрос не начат, safe retry: 429/503 у laozhang, 429 и 5xx у OpenAI
            # (OpenAI сам советует повторять rate limit и server errors с паузой).
            transient = resp.status_code in (429, 503) or (provider == "openai" and resp.status_code >= 500)
            if transient:
                err = error_text(resp)
                log_failed(job_id, err, billed=False)
                if retries < max_safe_retries:
                    retries += 1
                    delay = (20 if provider == "openai" and resp.status_code == 429 else 5) * (1 if retries == 1 else 3)
                    print(f"  [{resp.status_code}] retry {retries}/{max_safe_retries} через {delay} сек...")
                    time.sleep(delay)
                    job_id = f"{job_id}_r{retries}"  # новый job_id для retry
                    continue
                if go_openai(str(resp.status_code)):
                    continue
                log_prompt_md(**md, status="failed", error=err)
                raise NotBilledError(f"{err} — после {max_safe_retries} попыток")

            # laozhang не достучался до линии (do_request_failed): ответ пришёл, не тарифицирован.
            if provider == "laozhang" and resp.status_code >= 500 and "do_request_failed" in resp.text:
                err = error_text(resp)
                log_failed(job_id, err, billed=False)
                log_prompt_md(**md, status="failed", error=err)
                if model in WEB_FALLBACK and not fell_back:
                    fell_back = True
                    model = WEB_FALLBACK[model]
                    job_id, retries = f"{job_id}_fb", 0
                    print(f"  [500 do_request_failed] линия лежит, повторяю на {model} (не тарифицировано)")
                    continue
                if go_openai("500 do_request_failed"):
                    continue
                raise NotBilledError(f"{err} (линия недоступна, не тарифицировано)")

            if resp.status_code >= 400:
                err = error_text(resp)
                # 4xx провайдер отвергает до генерации (size, модерация) — не тарифицируется.
                # 5xx laozhang после отправки считаем неизвестно-оплаченным, как таймаут.
                billed = resp.status_code >= 500
                log_failed(job_id, err, billed=billed)
                log_prompt_md(**md, status="unknown_billed" if billed else "failed", error=err)
                raise RuntimeError(err) if billed else NotBilledError(err)

            data = resp.json()
            if provider == "openai" and data.get("usage"):
                cost = openai_cost(data["usage"])
                log_billed(job_id, provider, model, data["usage"], cost)
            ACTUAL_COSTS[caller_job_id] = cost
            img_bytes = extract_image_bytes(data)
            output_path = save_compressed(
                img_bytes, output_path.name, project,
                target=None if keep_2k else DELIVERY_SIZES.get(aspect_ratio),
            )
            md_path = log_prompt_md(**md, status="success", output=str(output_path),
                                    elapsed=time.time() - t0, cost=cost)
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


def main():
    parser = argparse.ArgumentParser(description="image-gen — генератор одиночных изображений")
    parser.add_argument("--prompt", required=True, help="Промт для генерации")
    parser.add_argument("--ratio", default="1:1", help="Aspect ratio (default: 1:1)")
    parser.add_argument("--size", default="2K", help="Обычно не нужен: размер берётся из --ratio. "
                                                     "Явный ШxВ (например 2160x3840) уходит в API как есть; стороны кратны 16")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"{', '.join(MODELS)} (default: {DEFAULT_MODEL}; gpt-image-2 только по явному указанию)")
    parser.add_argument("--quality", default=None, choices=QUALITIES,
                        help=f"low/medium/high/xhigh/max; без флага у laozhang не передаётся, "
                             f"у OpenAI — {OPENAI_DEFAULT_QUALITY}")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=PROVIDERS,
                        help="laozhang (по умолчанию, $0.03) или openai (официальный API, по токенам)")
    parser.add_argument("--background", default=None, choices=("transparent", "opaque"),
                        help="transparent — PNG с альфой (OpenAI и vip-линии laozhang)")
    parser.add_argument("--moderation", default=None, choices=("auto", "low"),
                        help="только OpenAI: low — мягче фильтр (тело, бикини, медицина)")
    parser.add_argument("--keep-2k", action="store_true",
                        help="не уменьшать до размера доставки, оставить оригинал 2K "
                             "(нужно, если картинка ложится на холст шире 1080)")
    parser.add_argument("--name", default="", help="Имя файла без расширения")
    parser.add_argument("--ref", nargs="+", default=None, help="Референсные изображения (image-to-image через /images/edits)")
    parser.add_argument("--project", default=None,
                        help="Проект/клиент, например acme/2026-09-16-launch: промпт запишется в "
                             "<IMAGE_GEN_DATA_DIR>/creatives/<project>/prompts.md")

    args = parser.parse_args()

    model = normalize_model(args.model, args.provider)

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
    quality = args.quality or (OPENAI_DEFAULT_QUALITY if args.provider == "openai" else None)
    gpt_size = resolve_gpt_size(args.size, args.ratio, args.provider)
    cost = get_cost(model, args.provider, quality, len(args.ref or []), gpt_size)
    job_id = f"single_{int(time.time())}_{args.name or 'img'}"
    print(f"Модель: {args.model} → {args.provider}/{model}")
    print(f"Размер: {gpt_size} (ratio: {args.ratio})" + (f", quality: {quality}" if quality else ""))

    if args.ref:
        print(f"Референсы: {len(args.ref)} изображений")
        for r in args.ref:
            print(f"  - {r}")

    print(f"Стоимость: ${cost:.3f}" + (" (оценка, факт по токенам после ответа)" if args.provider == "openai" else ""))
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
        provider=args.provider,
        background=args.background,
        moderation=args.moderation,
    )
    elapsed = time.time() - t0

    log_success(job_id, str(output_path), elapsed)

    ref_copy = REFS_DIR / output_path.name
    shutil.copy2(output_path, ref_copy)

    print(f"✓ Готово: {output_path.name} ({elapsed:.0f} сек, ${ACTUAL_COSTS.get(job_id, cost):.3f})")
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
