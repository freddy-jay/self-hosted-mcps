"""Validate identifiers before they reach paths, Podman options or build files."""
import re
from pathlib import Path


def server_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?", name):
        raise ValueError("server name must be 1-59 lowercase letters, digits or hyphens, starting and ending with a letter or digit")
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("server name is reserved by Windows")
    if "env" in name.split("-"):
        raise ValueError("server names cannot contain an 'env' segment: it is reserved for environment secrets")
    return name


def env_name(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not key.strip("_"):
        raise ValueError("environment names must use letters, digits and underscores")
    return key


def source_path(root: Path, name: str) -> Path:
    server_name(name)
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("source directory must stay directly inside the managed source root")
    return path


def workdir(value: str) -> str:
    if value == ".":
        return value
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value) or ".." in value.split("/"):
        raise ValueError("--subdir must be a relative directory inside the repository")
    return value


def build_file(context: Path) -> Path:
    path = context / "Containerfile.mcps"
    if path.is_symlink() or path.resolve().parent != context.resolve():
        raise ValueError("Containerfile.mcps must not point outside the build context")
    return path
