#!/usr/bin/env python3
"""Block commits that would publish personal data (adapted from nari).

Checks the files staged for commit (or, with --all, every tracked file) against:
  * every value in `.env` with 8+ characters (API keys, WeCom secrets, home/work coordinates and
    addresses, user ids …) — except values that also appear in `.env.example` (public defaults such as
    `Asia/Shanghai` or a cron string);
  * the server address in `.deploy.env` (the IP/host on its own, too);
  * regex patterns in the git-ignored `.privacy-patterns` file (one per line, `#` comments; the patterns
    themselves are personal, so they never enter git) — e.g. family names, the neighbourhood, phone numbers.

Installed as a pre-commit hook by `scripts/install-hooks.sh`. Exit code 1 lists each hit (never the value).
Run by hand: python3 scripts/privacy_check.py [--all]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_FILE = ROOT / ".privacy-patterns"
MIN_LEN = 8


def env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            out[key.strip().removeprefix("export ").strip()] = value
    return out


def load_secrets() -> dict[str, str]:
    public = set(env_values(ROOT / ".env.example").values())
    secrets = {f".env {k}": v for k, v in env_values(ROOT / ".env").items() if len(v) >= MIN_LEN and v not in public}
    for k, v in env_values(ROOT / ".deploy.env").items():
        secrets[f".deploy.env {k}"] = v
        host = v.rpartition("@")[2]
        if host != v:
            secrets[f".deploy.env {k} (host)"] = host
    return secrets


def load_patterns() -> list[re.Pattern]:
    if not PATTERNS_FILE.exists():
        return []
    out = []
    for n, line in enumerate(PATTERNS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line))
            except re.error as e:
                sys.exit(f"{PATTERNS_FILE.name}:{n}: bad regex {line!r}: {e}")
    return out


def files_to_check(all_files: bool) -> list[str]:
    cmd = ["git", "ls-files", "-z"] if all_files else ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [f for f in out.split("\0") if f and (ROOT / f).is_file()]


def file_text(path: str, all_files: bool) -> str:
    if all_files:
        return (ROOT / path).read_text(encoding="utf-8", errors="ignore")
    # what will actually be committed, not the working-tree copy
    return subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="ignore").stdout


def main() -> int:
    all_files = "--all" in sys.argv
    patterns, secrets = load_patterns(), load_secrets()
    if not patterns:
        print(f"privacy_check: no {PATTERNS_FILE.name} — only .env / .deploy.env values are checked", file=sys.stderr)
    hits = []
    for f in files_to_check(all_files):
        text = file_text(f, all_files)
        for pat in patterns:
            if m := pat.search(text):
                hits.append(f"{f}:{text.count(chr(10), 0, m.start()) + 1}: matches a .privacy-patterns entry ({pat.pattern!r})")
        for name, value in secrets.items():
            if (i := text.find(value)) >= 0:
                hits.append(f"{f}:{text.count(chr(10), 0, i) + 1}: contains the value of {name}")
    if hits:
        print("privacy_check: personal data in files about to be committed:\n  " + "\n  ".join(hits), file=sys.stderr)
        print("Fix it, or (if it's a false positive) refine .privacy-patterns.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
