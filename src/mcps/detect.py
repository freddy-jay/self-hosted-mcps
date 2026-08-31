"""Turn a target (GitHub repo, npm package, PyPI package) into build inputs."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import SRC_DIR

VENV = "/opt/venv"


def py_install(target: str, requires_python: str = "") -> str:
    """uv fetches the interpreter the project asks for, rather than the base image's."""
    version = f' --python "{requires_python}"' if requires_python else ""
    return f"uv venv{version} {VENV} && VIRTUAL_ENV={VENV} uv pip install {target}"


class DetectError(RuntimeError):
    pass


def force_rmtree(path: Path) -> None:
    """rmtree that survives the read-only files git leaves under .git on Windows."""
    def clear_readonly(func, target, _exc):
        import os
        import stat

        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=clear_readonly)


@dataclass
class Source:
    name: str
    origin: str
    context: Path      # build context: the repo root, so monorepo configs resolve
    workdir: str = "."  # path inside the repo the server actually lives at

    @property
    def root(self) -> Path:
        return self.context / self.workdir


def default_name(target: str) -> str:
    if target.startswith(("npm:", "pypi:")):
        stem = target.split(":", 1)[1]
    else:
        stem = re.sub(r"\.git$", "", target.rstrip("/")).rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-")
    return slug or "mcp"


def fetch(target: str, name: str, ref: str | None = None, subdir: str | None = None) -> Source:
    context = SRC_DIR / name
    if context.exists():
        force_rmtree(context)
    context.parent.mkdir(parents=True, exist_ok=True)

    if target.startswith(("npm:", "pypi:")):
        context.mkdir(parents=True)
        (context / ".mcps-package").write_text(target, encoding="utf-8")
        return Source(name=name, origin=target, context=context)

    url = target if "://" in target or target.startswith("git@") else f"https://github.com/{target}"
    clone = ["git", "clone", "--depth", "1", "--recurse-submodules"]
    if ref:
        clone += ["--branch", ref]
    proc = subprocess.run([*clone, url, str(context)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise DetectError(f"clone failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else url}")
    force_rmtree(context / ".git")

    workdir = (subdir or ".").strip("/")
    if not (context / workdir).is_dir():
        raise DetectError(f"--subdir {subdir} not found in {url}")
    return Source(name=name, origin=url, context=context, workdir=workdir or ".")


def detect(source: Source) -> tuple[str, str]:
    """Return (install_command, run_command) to bake into the image."""
    if source.origin.startswith("npm:"):
        pkg = source.origin.split(":", 1)[1]
        return f"npm install -g {pkg}", f"npx -y {pkg}"
    if source.origin.startswith("pypi:"):
        pkg = source.origin.split(":", 1)[1]
        return f"uv tool install {pkg}", f"uvx {pkg}"

    root = source.root
    if (root / "package.json").exists():
        return _node(root)
    if (root / "pyproject.toml").exists():
        return _python(root)
    if (root / "requirements.txt").exists():
        return _requirements(root)
    raise DetectError(
        "no package.json, pyproject.toml or requirements.txt found. "
        "Point --subdir at the server, or pass --cmd with the stdio command to run."
    )


def _node(root: Path) -> tuple[str, str]:
    data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    install = "npm install"
    if (root / "package-lock.json").exists():
        install = "npm ci || npm install"
    if (data.get("scripts") or {}).get("build"):
        install += " && npm run build"

    entry = data.get("bin")
    if isinstance(entry, dict):
        entry = next(iter(entry.values()), None)
    entry = entry or data.get("main")
    if not entry:
        raise DetectError("package.json has no `bin` or `main`; pass --cmd with the stdio command to run.")
    return install, f"node {str(entry).lstrip('./')}"


def _python(root: Path) -> tuple[str, str]:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project") or {}
    install = py_install(".", project.get("requires-python", ""))
    scripts = project.get("scripts") or {}
    if scripts:
        return install, f"{VENV}/bin/{next(iter(scripts))}"
    name = project.get("name")
    if not name:
        raise DetectError("pyproject.toml has no [project.scripts] or name; pass --cmd.")
    return install, f"{VENV}/bin/python -m {name.replace('-', '_')}"


def _requirements(root: Path) -> tuple[str, str]:
    install = py_install("-r requirements.txt")
    for candidate in ("server.py", "main.py", "app.py", "__main__.py"):
        if (root / candidate).exists():
            return install, f"{VENV}/bin/python {candidate}"
    raise DetectError("requirements.txt found but no obvious entrypoint; pass --cmd.")
