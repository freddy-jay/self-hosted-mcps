"""Reads the host tailscale client, only to learn the tailnet's MagicDNS suffix."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# Where each OS installs the client by default. MCPS_TAILSCALE overrides all of it.
CANDIDATES = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
]


def _binary() -> str | None:
    override = os.environ.get("MCPS_TAILSCALE")
    if override:
        return override if Path(override).exists() else shutil.which(override)
    on_path = shutil.which("tailscale")
    if on_path:
        return on_path
    return next((path for path in CANDIDATES if Path(path).exists()), None)


def magic_dns_suffix() -> str | None:
    binary = _binary()
    if not binary:
        return None
    proc = subprocess.run([binary, "status", "--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    suffix = status.get("MagicDNSSuffix")
    if suffix:
        return suffix.strip(".")
    dns = (status.get("Self") or {}).get("DNSName", "")
    return dns.strip(".").split(".", 1)[1] if "." in dns else None
