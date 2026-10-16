"""Append-only activity log (data/activity.jsonl) — the source of the daily admin report.

One JSON object per line: {"ts": epoch, "type": ..., ...fields}. Survives restarts (unlike the
in-memory GLM counters). Entries older than KEEP_DAYS are dropped when the report is built.

Types: sent · chat · on_demand · llm · extract · cleanup · wemp_notice · feedback · error
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
KEEP_DAYS = 8


class ActivityLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, type_: str, **fields: Any) -> None:
        """Best effort: logging activity must never break a message."""
        entry = {"ts": round(self._clock(), 1), "type": type_, **fields}
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.warning("activity log write failed: %s", exc)

    def since(self, seconds: float) -> list[dict[str, Any]]:
        cutoff = self._clock() - seconds
        return [e for e in self._read() if e.get("ts", 0) >= cutoff]

    def prune(self) -> None:
        cutoff = self._clock() - KEEP_DAYS * 86400
        with self._lock:
            kept = [e for e in self._read_unlocked() if e.get("ts", 0) >= cutoff]
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("activity log prune failed: %s", exc)

    def _read(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn line from a crash — skip it
        return out
