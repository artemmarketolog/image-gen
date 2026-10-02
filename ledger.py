"""
Ledger — журнал всех запросов к image-gen API.
Каждый POST логируется ДО отправки (started), потом обновляется (success/failed/unknown_billed).
Формат: JSONL, один файл, append-only. Промпт пишется целиком.

Рядом ведётся человекочитаемый журнал промптов (Markdown), чтобы любой промпт
можно было найти и переиспользовать без разбора JSONL:
  без --project → <data>/image-gen/prompts/YYYY-MM.md
  с --project X → <data>/creatives/X/prompts.md (папка проекта клиента)

До 2026-09-10 журнал жил в /tmp/image_gen_ledger.jsonl с промптом, обрезанным до
200 символов, и стирался при перезагрузке (tmpfiles: D /tmp 30d).
"""

import json
import time
from pathlib import Path

from skill_config import DATA_DIR

STATE_DIR = DATA_DIR
LEDGER_DIR = STATE_DIR / "image-gen"
LEDGER_PATH = LEDGER_DIR / "ledger.jsonl"
PROMPTS_DIR = LEDGER_DIR / "prompts"
CREATIVES_DIR = STATE_DIR / "creatives"


def _write_line(entry: dict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_started(job_id: str, prompt: str, model: str, ratio: str, name: str, cost: float, **fields) -> None:
    """Записать ДО отправки запроса. fields: size, quality, refs, project, requested_model."""
    _write_line({
        "job_id": job_id,
        "status": "started",
        "prompt": prompt,
        "model": model,
        "ratio": ratio,
        "name": name,
        "cost": cost,
        **fields,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def log_success(job_id: str, output_path: str, elapsed: float) -> None:
    _write_line({
        "job_id": job_id,
        "status": "success",
        "output": output_path,
        "elapsed_sec": round(elapsed),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def log_failed(job_id: str, error: str, billed: bool) -> None:
    """billed=True → unknown_billed (запрос ушёл, ответ не дошёл). billed=False → safe fail."""
    _write_line({
        "job_id": job_id,
        "status": "unknown_billed" if billed else "failed",
        "error": str(error)[:600],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def prompts_md_path(project: str | None) -> Path:
    if project:
        return CREATIVES_DIR / project / "prompts.md"
    return PROMPTS_DIR / (time.strftime("%Y-%m") + ".md")


def log_prompt_md(
    *, name: str, prompt: str, model: str, size: str, ratio: str, status: str,
    output: str | None = None, quality: str | None = None, refs: list[str] | None = None,
    project: str | None = None, elapsed: float | None = None, error: str | None = None,
) -> Path:
    """Дописать одну генерацию в Markdown-журнал промптов. Возвращает путь к файлу."""
    path = prompts_md_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [model, f"{size} ({ratio})"]
    if quality:
        meta.append(f"quality {quality}")
    if elapsed is not None:
        meta.append(f"{elapsed:.0f} с")
    lines = [f"### {time.strftime('%Y-%m-%d %H:%M')} · {name} · {status}", "", "- " + " · ".join(meta)]
    if refs:
        lines.append("- refs: " + ", ".join(refs))
    if output:
        lines.append(f"- файл: {output}")
    if error:
        lines.append(f"- ошибка: {error[:300]}")
    lines += ["", "```", prompt.strip(), "```", ""]
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")
    return path
