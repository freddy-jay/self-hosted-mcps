"""Validate identifiers before they reach paths, Podman options or build files."""
import re
from pathlib import Path


def server_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?", name):
        raise ValueError("use a name like 'my-server': lowercase letters, digits and hyphens, up to 59 characters")
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError(f"'{name}' is reserved by Windows; use '{name}-mcp' instead")
    if "env" in name.split("-"):
        raise ValueError("'env' is reserved in server names; use 'environment' instead")
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
    value = value.replace("\\", "/")
    if value.startswith("/"):
        raise ValueError("use a folder inside the repository, for example --subdir src/server")
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/") or "."
    if value == ".":
        return value
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value) or ".." in value.split("/"):
        raise ValueError("use a folder inside the repository, for example --subdir src/server")
    return value


def build_file(context: Path) -> Path:
    path = context / "Containerfile.mcps"
    if path.is_symlink() or path.resolve().parent != context.resolve():
        raise ValueError("Containerfile.mcps must not point outside the build context")
    return path
