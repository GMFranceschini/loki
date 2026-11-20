"""Filesystem layout. Everything lives under the project root."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("LOKI_ROOT", Path(__file__).resolve().parent.parent))

CONFIG_DIR = ROOT / "config"
INTERESTS_FILE = CONFIG_DIR / "interests.yaml"
JOURNALS_FILE = CONFIG_DIR / "journals.json"

DATA_DIR = Path(os.environ.get("LOKI_DATA", ROOT / "data"))
DB_FILE = DATA_DIR / "loki.db"
PID_FILE = DATA_DIR / "server.pid"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"

WEB_DIR = ROOT / "web"


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
