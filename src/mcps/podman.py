"""Thin wrapper around the podman CLI. Podman labels are the source of truth."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from . import validation

RUNTIME = Path(__file__).parent / "runtime"
BASE_IMAGE = os.environ.get("MCPS_BASE_IMAGE", "localhost/mcps-base:2")
TS_IMAGE = os.environ.get("MCPS_TS_IMAGE", "docker.io/tailscale/tailscale:latest")
POD_PREFIX = "mcps-"


class PodmanError(RuntimeError):
    pass


def run(*args: str, check: bool = True, capture: bool = True, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["podman", *args],
        input=stdin,
        capture_output=capture,
        text=True,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise PodmanError(f"podman {' '.join(args)} failed:\n{err}")
    return (proc.stdout or "").strip()


def stream(*args: str) -> int:
    return subprocess.run(["podman", *args]).returncode


def preflight() -> None:
    if not shutil.which("podman"):
        raise PodmanError("podman is not on PATH. Install Podman Desktop, then run: podman machine start")
    proc = subprocess.run(["podman", "info", "--format", "{{.Host.OS}}"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PodmanError("podman is not reachable. Start it with: podman machine start")


def image_exists(ref: str) -> bool:
    return subprocess.run(["podman", "image", "exists", ref], capture_output=True).returncode == 0


def ensure_base_image(rebuild: bool = False) -> None:
    if image_exists(BASE_IMAGE) and not rebuild:
        return
    if stream("build", "-t", BASE_IMAGE, "-f", str(RUNTIME / "base.Containerfile"), str(RUNTIME)) != 0:
        raise PodmanError("shared runtime image build failed")


def pod_name(name: str) -> str:
    validation.server_name(name)
    return f"{POD_PREFIX}{name}"


def pod_exists(name: str) -> bool:
    return subprocess.run(["podman", "pod", "exists", pod_name(name)], capture_output=True).returncode == 0


def list_pods() -> list[dict]:
    out = run("pod", "ps", "--format", "json")
    pods = json.loads(out) if out else []
    return [p for p in pods if (p.get("Labels") or {}).get("mcps.name")]


def destroy(name: str) -> None:
    # Log out first so the control plane drops the node immediately. Without this
    # the old ephemeral node lingers, and the replacement takes the next free
    # hostname - mcp-<name>-1, then -2 - changing the URL on every rebuild.
    subprocess.run(
        ["podman", "exec", f"{pod_name(name)}-ts", "tailscale", "logout"],
        capture_output=True, timeout=30,
    )
    subprocess.run(["podman", "pod", "rm", "-f", pod_name(name)], capture_output=True)
    subprocess.run(["podman", "volume", "rm", "-f", f"mcps-ts-{name}"], capture_output=True)


def write_serve_config(volume: str, serve_json: str) -> None:
    """Seed the tailscale state volume with a serve config, no host bind mounts."""
    run("volume", "create", volume, check=False)
    run(
        "run", "--rm", "-i", "-v", f"{volume}:/state",
        "--entrypoint", "sh", TS_IMAGE, "-c", "cat > /state/serve.json",
        stdin=serve_json,
    )


def secret_name(server: str, kind: str) -> str:
    validation.server_name(server)
    return f"mcps-{server}-{kind}"


def secret_set(name: str, value: str) -> None:
    """Create or replace a secret. Podman refuses to overwrite, so remove first."""
    subprocess.run(["podman", "secret", "rm", name], capture_output=True)
    run("secret", "create", name, "-", stdin=value)


def secret_get(name: str) -> str:
    proc = subprocess.run(
        ["podman", "secret", "inspect", name, "--showsecret", "--format", "{{.SecretData}}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def secret_rm(name: str) -> None:
    subprocess.run(["podman", "secret", "rm", name], capture_output=True)


def machine_exists() -> bool:
    """Podman on Windows and macOS runs containers inside a VM that must be up first."""
    out = run("machine", "list", "--format", "json", check=False)
    try:
        return bool(json.loads(out)) if out else False
    except json.JSONDecodeError:
        return False


def machine_ssh(command: str, check: bool = True) -> str:
    return run("machine", "ssh", command, check=check)


def cli_path() -> str:
    return shutil.which("podman") or "podman"


def env_secret_name(server: str, key: str) -> str:
    validation.server_name(server)
    validation.env_name(key)
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return f"mcps-{server}-env-{slug}"


def secret_names(prefix: str) -> list[str]:
    # `podman secret ls` has no json formatter; it reads --format as a Go template.
    out = run("secret", "ls", "--format", "{{.Name}}", check=False)
    return [line.strip() for line in out.splitlines() if line.strip().startswith(prefix)]
