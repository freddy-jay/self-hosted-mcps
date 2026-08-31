"""On-disk state: a small JSON config, and nothing else.

No secret is written here. The Tailscale auth key, each server's bearer token
and every variable passed with -e / --env-file live in podman secrets, which
stay masked in `podman inspect` and never touch the host filesystem.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("MCPS_HOME") or (Path.home() / ".mcps"))
CONFIG_PATH = HOME / "config.json"
SRC_DIR = HOME / "src"

AUTHKEY_SECRET = "mcps-authkey"

DEFAULTS = {"tailnet": "", "https": True, "ts_extra_args": ""}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({k: v for k, v in cfg.items() if k != "authkey"}, indent=2),
        encoding="utf-8",
    )
