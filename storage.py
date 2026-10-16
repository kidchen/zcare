"""Tiny JSON state files (recent picks, preferences, article extracts) + nightly backups.

Safety (a corrupt file must never silently wipe history):
  * every write is atomic (tmp + os.replace) and first copies the current good file to ``<name>.bak``;
  * a file that can't be parsed is moved aside to ``<name>.corrupt-<timestamp>`` (kept for manual
    recovery, never overwritten) and the ``.bak`` copy is restored; only if that is unusable too does
    the store start empty. Either way ``on_problem`` is called (→ the admin's daily report).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BACKUP_FILES = ("state.json", "article_extracts.json", "activity.jsonl")  # details/ & posters/ are regenerable
BACKUP_KEEP = 7


class StateStore:
    def __init__(self, path: Path, on_problem: Callable[[str], None] | None = None):
        self._path = Path(path)
        self._bak = self._path.with_name(self._path.name + ".bak")
        self._lock = threading.Lock()
        self.on_problem = on_problem

    def _read(self, path: Path) -> dict[str, Any] | None:
        """Parsed dict, or None if missing / unreadable / not a JSON object."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return self._read(self._bak) or {}  # crash between writes: fall back to the last good copy
        data = self._read(self._path)
        if data is not None:
            return data
        aside = self._path.with_name(f"{self._path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(self._path, aside)
        except OSError as exc:
            log.error("cannot move corrupt %s aside: %s", self._path, exc)
        restored = self._read(self._bak)
        msg = (f"{self._path.name} was corrupt (kept as {aside.name}); "
               + ("restored the previous copy" if restored is not None else "no usable backup — starting empty"))
        log.error(msg)
        if self.on_problem is not None:
            try:
                self.on_problem(msg)
            except Exception:  # noqa: BLE001 — reporting must never break state access
                log.exception("on_problem hook failed")
        if restored is not None:
            self._write(restored, backup=False)
        return restored or {}

    def _write(self, data: dict[str, Any], backup: bool = True) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if backup and self._path.exists():
            shutil.copyfile(self._path, self._bak)  # only reached after _load() parsed it → known good
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # atomic on POSIX

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data[key] = value
            self._write(data)

    def recent(self, key: str) -> list[str]:
        return list(self.get(key, []))

    def remember(self, key: str, items: list[str], keep: int) -> None:
        merged = [i for i in self.recent(key) if i not in items] + list(items)
        self.set(key, merged[-keep:])


def backup_data(data_dir: Path, today: date, keep: int = BACKUP_KEEP) -> Path | None:
    """Copy the irreplaceable data files into ``data/backups/<YYYYMMDD>/``; keep the newest ``keep`` days.
    Returns the backup directory, or None if there was nothing to copy."""
    data_dir = Path(data_dir)
    files = [data_dir / n for n in BACKUP_FILES if (data_dir / n).is_file()]
    if not files:
        return None
    root = data_dir / "backups"
    dest = root / today.strftime("%Y%m%d")
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        tmp = dest / (f.name + ".tmp")
        shutil.copyfile(f, tmp)
        os.replace(tmp, dest / f.name)
    days = sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 8)
    for old in days[: max(0, len(days) - keep)]:
        shutil.rmtree(old, ignore_errors=True)
    return dest
