#!/usr/bin/env python3
"""
batch_generate.py — контролируемая параллельная генерация изображений.

Один процесс, один global lock, ThreadPoolExecutor с ограничением.
Каждый запрос логируется в ledger ДО отправки.
Zero-retry на таймаут/disconnect (unknown_billed).
В конце — отчёт: success / unknown_billed / failed + стоимость.

Использование:
  python3 batch_generate.py --jobs jobs.jsonl [--parallel 1]
  python3 batch_generate.py --prompts "prompt1" "prompt2" --names "name1" "name2" [--ratio 9:16] [--parallel 1]

jobs.jsonl формат (одна строка = одна генерация):
  {"prompt": "...", "name": "file1", "ratio": "9:16"}
  {"prompt": "...", "name": "file2", "ratio": "9:16", "model": "gpt-image-2", "quality": "high", "project": "acme/2026-09-16-launch"}
Без "model" — дефолт скилла (gpt-image-2.5). "project" пишет промпт в <data>/creatives/<project>/prompts.md.
"""

import argparse
import json
import sys
import time
import fcntl
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Добавляем parent в path для импорта
sys.path.insert(0, str(Path(__file__).parent))

from generate import (
    generate_image, get_cost, normalize_model, DEFAULT_MODEL,
    OUTPUT_DIR, REFS_DIR, LOCK_FILE,
)
from ledger import log_started, log_success, log_failed

MAX_PARALLEL = 4  # жёсткий потолок; 3 параллельных на 2.5 проверены 10.09.2026 без 429


def run_one_job(job: dict, batch_id: str, index: int) -> dict:
    """Запускает одну генерацию, возвращает результат."""
    prompt = job["prompt"]
    name = job.get("name", f"batch_{batch_id}_{index}")
    ratio = job.get("ratio", "1:1")
    model = job.get("model") or DEFAULT_MODEL
    size = job.get("size", "2K")
    ref = job.get("ref")
    quality = job.get("quality")
    project = job.get("project")
    allow_no_ref_fallback = bool(job.get("allow_no_ref_fallback"))

    job_id = f"batch_{batch_id}_{index}_{name}"
    output_path = OUTPUT_DIR / f"{name}.png"

    # Гейт моделей — тот же, что в generate.py (дефолт 2.5, gpt-image-2 по явному указанию).
    try:
        normalize_model(model)
    except SystemExit as e:
        print(f"  [{index + 1}] ⛔ {name} — {e}")
        return {
            "job_id": job_id, "name": name, "cost": 0.0, "status": "failed",
            "output": None, "error": str(e), "elapsed": 0,
        }

    cost = get_cost(model)

    result = {
        "job_id": job_id,
        "name": name,
        "cost": cost,
        "status": None,
        "output": None,
        "error": None,
        "elapsed": 0,
    }

    print(f"  [{index + 1}] Генерирую: {name} (${cost})...")

    t0 = time.time()
    try:
        output_path = generate_image(
            prompt=prompt,
            output_path=output_path,
            model=model,
            size=size,
            aspect_ratio=ratio,
            ref_images=ref,
            job_id=job_id,
            quality=quality,
            project=project,
            keep_2k=bool(job.get("keep_2k", False)),
        )
        elapsed = time.time() - t0
        log_success(job_id, str(output_path), elapsed)

        # Копия в refs
        ref_copy = REFS_DIR / output_path.name
        shutil.copy2(output_path, ref_copy)

        result["status"] = "success"
        result["output"] = str(output_path)
        result["elapsed"] = round(elapsed)
        print(f"  [{index + 1}] ✓ {name} ({elapsed:.0f} сек)")
        return result

    except Exception as e:
        if allow_no_ref_fallback and ref and "503" in str(e):
            print(f"  [{index + 1}] ↪ refs/edit вернул 503, пробую text-to-image без референсов...")
            try:
                output_path = generate_image(
                    prompt=prompt,
                    output_path=output_path,
                    model=model,
                    size=size,
                    aspect_ratio=ratio,
                    ref_images=None,
                    job_id=f"{job_id}_no_ref",
                    quality=quality,
                    project=project,
                    keep_2k=bool(job.get("keep_2k", False)),
                )
                elapsed = time.time() - t0
                log_success(f"{job_id}_no_ref", str(output_path), elapsed)

                # Копия в refs
                ref_copy = REFS_DIR / output_path.name
                shutil.copy2(output_path, ref_copy)

                result["status"] = "success"
                result["output"] = str(output_path)
                result["elapsed"] = round(elapsed)
                print(f"  [{index + 1}] ✓ {name} без refs ({elapsed:.0f} сек)")
                return result
            except Exception as fallback_error:
                e = fallback_error

        elapsed = time.time() - t0
        result["elapsed"] = round(elapsed)
        result["error"] = str(e)[:200]

        # Если таймаут или connection error после отправки — unknown_billed
        err_type = type(e).__name__
        if "Timeout" in err_type or ("Connection" in err_type and elapsed > 5) or ("HTTP 5" in str(e) and "do_request_failed" not in str(e)):
            result["status"] = "unknown_billed"
            print(f"  [{index + 1}] ⚠️ {name} — таймаут/disconnect ({elapsed:.0f} сек), возможно тарифицирован")
        else:
            result["status"] = "failed"
            print(f"  [{index + 1}] ✗ {name} — {err_type}: {str(e)[:100]}")

        return result


def main():
    parser = argparse.ArgumentParser(description="batch image-gen — контролируемая параллельная генерация")
    parser.add_argument("--jobs", help="Путь к JSONL файлу с заданиями")
    parser.add_argument("--prompts", nargs="+", help="Промты (альтернатива --jobs)")
    parser.add_argument("--names", nargs="+", help="Имена файлов (с --prompts)")
    parser.add_argument("--ratio", default="1:1", help="Ratio для всех (с --prompts)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Модель для всех (с --prompts), default: {DEFAULT_MODEL}")
    parser.add_argument("--quality", default=None, help="low/medium/high/xhigh/max для всех задач без своего quality")
    parser.add_argument("--project", default=None, help="Проект для всех задач без своего project (<data>/creatives/<project>/prompts.md)")
    parser.add_argument("--parallel", type=int, default=0, help=f"Параллельных воркеров (default: auto = min(jobs, {MAX_PARALLEL}), max: {MAX_PARALLEL})")

    args = parser.parse_args()

    # Собираем jobs
    jobs = []
    if args.jobs:
        with open(args.jobs) as f:
            for line in f:
                line = line.strip()
                if line:
                    jobs.append(json.loads(line))
    elif args.prompts:
        names = args.names or [f"batch_{i}" for i in range(len(args.prompts))]
        if len(names) < len(args.prompts):
            names.extend([f"batch_{i}" for i in range(len(names), len(args.prompts))])
        for prompt, name in zip(args.prompts, names):
            jobs.append({"prompt": prompt, "name": name, "ratio": args.ratio, "model": args.model})
    else:
        print("⛔ Укажи --jobs или --prompts")
        sys.exit(1)

    if not jobs:
        print("⛔ Нет заданий")
        sys.exit(1)
    for job in jobs:
        job.setdefault("quality", args.quality)
        job.setdefault("project", args.project)

    parallel = min(len(jobs), MAX_PARALLEL) if args.parallel == 0 else min(max(args.parallel, 1), MAX_PARALLEL)
    batch_id = str(int(time.time()))
    total_cost = sum(get_cost(j.get("model")) for j in jobs)

    print(f"📦 Batch: {len(jobs)} генераций, --parallel {parallel}")
    print(f"💰 Макс. стоимость: ${total_cost:.2f}")
    print(f"🔒 Batch ID: {batch_id}")
    print()

    results = []
    if parallel == 1:
        # Последовательно — проще и безопаснее
        for i, job in enumerate(jobs):
            result = run_one_job(job, batch_id, i)
            results.append(result)
    else:
        # Параллельно через ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(run_one_job, job, batch_id, i): i for i, job in enumerate(jobs)}
            for future in as_completed(futures):
                results.append(future.result())

    # Сортируем по порядку
    results.sort(key=lambda r: r["job_id"])

    # Отчёт
    print()
    print("=" * 50)
    success = [r for r in results if r["status"] == "success"]
    billed = [r for r in results if r["status"] == "unknown_billed"]
    failed = [r for r in results if r["status"] == "failed"]

    billed_cost = sum(r["cost"] for r in success) + sum(r["cost"] for r in billed)
    print(f"✓ Успех: {len(success)}/{len(jobs)}")
    if billed:
        print(f"⚠️ Unknown billed: {len(billed)} (запрос ушёл, ответ не дошёл)")
        for r in billed:
            print(f"   - {r['name']}: {r['error'][:80]}")
    if failed:
        print(f"✗ Failed: {len(failed)} (не тарифицированы)")
    print(f"💰 Потрачено (min): ${sum(r['cost'] for r in success):.2f}")
    if billed:
        print(f"💰 Потрачено (max, с unknown): ${billed_cost:.2f}")
    print()

    # __FILE__ для успешных
    for r in results:
        if r["status"] == "success" and r["output"]:
            delivery_path = OUTPUT_DIR / Path(r["output"]).name
            shutil.copy2(r["output"], delivery_path)
            print(f"__FILE__:{delivery_path}")

    if billed:
        print()
        print(f"⚠️ {len(billed)} картинок не дошли, возможно оплачены.")
        print(f"   Перегенерировать ТОЛЬКО после подтверждения пользователя (проверьте историю запросов в кабинете laozhang).")

    if failed or billed:
        sys.exit(2)


if __name__ == "__main__":
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⛔ Другая генерация (generate.py или batch) уже запущена.")
        print("   Параллельный запуск ЗАПРЕЩЁН — каждый запрос тарифицируется.")
        sys.exit(1)
    try:
        main()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
