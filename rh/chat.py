"""GPT Pro jobs handed to a person: the brief goes out, the saved reply is the result."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path


def prompt(spec: dict) -> str:
    return spec["brief"]


def reply_path(cfg: dict, job_id: str) -> Path:
    return Path(cfg["home"]) / "runs" / job_id / "reply.md"


def read_reply(cfg: dict, job_id: str, limit: int = 200_000) -> str | None:
    try:
        with reply_path(cfg, job_id).open(encoding="utf-8", newline="") as file:
            return file.read(limit)
    except FileNotFoundError:
        return None


def save_reply(cfg, led, job_id: str, text: str, now: float) -> None:
    job = led.job(job_id)
    if job is None:
        raise ValueError(f"no job {job_id}")
    if job["spec"]["kind"] != "gpt-chat":
        raise ValueError("only a gpt-chat job takes a reply")
    if job["state"] != "handoff":
        raise ValueError(f"{job_id} is {job['state']}; a reply needs handoff")
    if not text.strip():
        raise ValueError("a reply must not be empty")
    dest = reply_path(cfg, job_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=dest.parent,
                                         prefix=".reply-", suffix=".tmp", delete=False) as file:
            tmp = Path(file.name)
            file.write(text)
        tmp.replace(dest)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    detail = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0][:200]
    led.finish(job_id, "done", {"route": "chat", "checks": [],
               "model_requested": job["spec"]["model"]["requested"], "model_served": None,
               "ended": now, "detail": detail})
    led.event("reply", job_id, chars=len(text))
    for item in led.inbox():
        if item["kind"] == "chat" and item["job"] == job_id:
            led.inbox_resolve(item["id"], "reply saved")
