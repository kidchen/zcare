"""Tiny JSON state file — remembers recent picks so recommendations don't repeat."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data[key] = value
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)  # atomic on POSIX

    def recent(self, key: str) -> list[str]:
        return list(self.get(key, []))

    def remember(self, key: str, items: list[str], keep: int) -> None:
        merged = [i for i in self.recent(key) if i not in items] + list(items)
        self.set(key, merged[-keep:])
