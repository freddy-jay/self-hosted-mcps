"""A service container beside the MCP server; they share the pod's loopback."""

import re
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from mcps import podman, validation

VOLUME_LABEL = "mcps.companion"

_HOST = (
    r"(?:localhost"
    r"|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"
)
_IMAGE = re.compile(
    _HOST
    + r"(?::\d{1,5})?"
    + r"(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+"
    + r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?"
    + r"(?:@sha256:[a-f0-9]{64})?"
)


class CompanionConfigurationError(ValueError):
    """Companion settings cannot safely be passed to Podman."""


@dataclass(frozen=True)
class Companion:
    image: str
    volumes: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()

    def to_meta(self) -> dict[str, object]:
        return {
            "companion_image": self.image,
            "companion_volumes": list(self.volumes),
            "companion_env_keys": list(self.env_keys),
        }


def validate_image(ref: object) -> str:
    # A registry host keeps Podman from prompting for a short-name registry,
    # and a leading hyphen can never be parsed as a Podman option.
    if not isinstance(ref, str) or len(ref) > 255 or not _IMAGE.fullmatch(ref):
        raise CompanionConfigurationError(
            "use a fully qualified companion image such as "
            "ghcr.io/owner/image:tag or localhost/name:tag"
        )
    return ref


def validate_mount(path: object) -> str:
    plain = path.rstrip("/") if isinstance(path, str) else ""
    parts = plain.split("/")[1:]
    if (
        not re.fullmatch(r"(?:/[A-Za-z0-9_.-]+)+", plain)
        or "." in parts
        or ".." in parts
    ):
        raise CompanionConfigurationError(
            "--companion-volume takes an absolute path inside the companion, "
            "for example /data"
        )
    return plain


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def volume_name(server: str, mount: str) -> str:
    # A slug never contains "--", so the last "--" always ends the server name.
    validation.server_name(server)
    return f"mcps-companion-{server}--{_slug(validate_mount(mount))}"


def env_secret_name(server: str, key: str) -> str:
    # Lives under the server's reserved "env" prefix so `mcps rm` removes it;
    # "--" keeps it apart from every server variable, whose slug has none.
    validation.server_name(server)
    try:
        validation.env_name(key)
    except ValueError as exc:
        raise CompanionConfigurationError(str(exc)) from None
    return f"mcps-{server}-env-companion--{_slug(key)}"


def parse_env(items: Sequence[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        # Podman refuses an empty secret, and by then the old pod is gone.
        if not value:
            raise CompanionConfigurationError(
                "--companion-env expects KEY=VALUE with a non-empty value"
            )
        pairs[key] = value
    return pairs


def build(
    image: object,
    *,
    volumes: Sequence[object] = (),
    env_keys: Collection[object] = (),
) -> Companion:
    mounts = tuple(dict.fromkeys(validate_mount(path) for path in volumes))
    if len({_slug(mount) for mount in mounts}) != len(mounts):
        raise CompanionConfigurationError(
            "two --companion-volume paths map to the same volume name"
        )
    keys: list[str] = []
    for key in env_keys:
        if not isinstance(key, str):
            raise CompanionConfigurationError("invalid companion environment name")
        env_secret_name("validation", key)
        keys.append(key)
    if len({_slug(key) for key in keys}) != len(set(keys)):
        raise CompanionConfigurationError(
            "two companion environment names map to the same Podman secret"
        )
    return Companion(
        image=validate_image(image),
        volumes=mounts,
        env_keys=tuple(sorted(set(keys))),
    )


def from_meta(meta: Mapping[str, object]) -> Companion | None:
    """Saved settings are re-validated: metadata is a file anyone can edit."""
    if "companion_image" not in meta:
        return None
    volumes = meta.get("companion_volumes", [])
    env_keys = meta.get("companion_env_keys", [])
    if not isinstance(volumes, list) or not isinstance(env_keys, list):
        raise CompanionConfigurationError(
            "invalid saved companion configuration; "
            "re-run add with --companion IMAGE or --no-companion"
        )
    return build(meta["companion_image"], volumes=volumes, env_keys=env_keys)


def resolve(
    saved: Companion | None,
    *,
    image: str,
    volumes: Sequence[str],
    env_keys: Collection[str],
    remove: bool,
) -> Companion | None:
    """Combine a rebuild's flags with what the server already had.

    No flags keeps the companion. --companion redefines its image and volumes
    from this command alone. Environment secrets persist either way.
    """
    if remove:
        if image or volumes or env_keys:
            raise CompanionConfigurationError(
                "use --no-companion on its own, without other companion options"
            )
        return None
    kept = saved.env_keys if saved else ()
    if image:
        return build(image, volumes=volumes, env_keys={*kept, *env_keys})
    if volumes:
        raise CompanionConfigurationError(
            "pass --companion IMAGE together with --companion-volume"
        )
    if saved is None:
        if env_keys:
            raise CompanionConfigurationError(
                "this server has no companion; pass --companion IMAGE"
            )
        return None
    return build(saved.image, volumes=saved.volumes, env_keys={*kept, *env_keys})


def ensure_image(image: str) -> None:
    if not podman.image_exists(validate_image(image)):
        podman.run("pull", image)


def with_stored_env(server: str, companion: Companion) -> Companion:
    """Drop names whose secret is gone, so metadata matches what gets mounted."""
    stored = tuple(
        key
        for key in companion.env_keys
        if podman.secret_get(env_secret_name(server, key))
    )
    return replace(companion, env_keys=stored)


def start(server: str, companion: Companion) -> None:
    pod = podman.pod_name(server)
    args: list[str] = []
    for key in companion.env_keys:
        args += ["--secret", f"{env_secret_name(server, key)},type=env,target={key}"]
    for mount in companion.volumes:
        volume = volume_name(server, mount)
        # Labelled up front: `podman run -v` would create it unlabelled, and
        # `mcps rm` finds a server's volumes by this label.
        podman.run(
            "volume",
            "create",
            "--ignore",
            "--label",
            f"{VOLUME_LABEL}={server}",
            volume,
        )
        args += ["-v", f"{volume}:{mount}"]
    podman.run(
        "run",
        "-d",
        "--pod",
        pod,
        "--name",
        f"{pod}-companion",
        "--restart",
        "always",
        "--security-opt=no-new-privileges",
        *args,
        validate_image(companion.image),
    )


def _state(server: str) -> tuple[bool, str]:
    running, _, restarts = podman.run(
        "container",
        "inspect",
        f"{podman.pod_name(server)}-companion",
        "--format",
        "{{.State.Running}} {{.RestartCount}}",
        check=False,
    ).partition(" ")
    return running == "true", restarts


def stays_running(server: str, *, fresh: bool, settle: float = 3.0) -> bool:
    """Is the companion up and not being restarted?

    `--restart always` keeps a crash-looping container "running" most of the
    time, so one sample is not enough: its restart count must also stand still,
    and be zero for a container that was only just created.
    """
    first = _state(server)
    time.sleep(settle)
    second = _state(server)
    if not (first[0] and second[0]) or first[1] != second[1]:
        return False
    return second[1] == "0" or not fresh


def remove_secrets(server: str, env_keys: Sequence[str]) -> None:
    for key in env_keys:
        podman.secret_rm(env_secret_name(server, key))


def volume_names(server: str) -> list[str]:
    validation.server_name(server)
    listed = podman.run(
        "volume",
        "ls",
        "--filter",
        f"label={VOLUME_LABEL}={server}",
        "--format",
        "{{.Name}}",
        check=False,
    )
    # Belt and braces: never trust the filter alone with another server's data.
    return [
        name
        for name in listed.split()
        if name.rpartition("--")[0] == f"mcps-companion-{server}"
    ]


def remove_volumes(server: str) -> None:
    """Delete a removed server's companion data. Rebuilds never call this."""
    for name in volume_names(server):
        podman.run("volume", "rm", "-f", name, check=False)
