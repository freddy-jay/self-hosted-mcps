"""A service container beside the MCP server; they share the pod's loopback."""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from mcps import podman, validation

# The pod already listens here: Tailscale serve, the gateway, the bridge and
# the tunnel health endpoint. Forwarding 8081 would bypass the bearer token.
RESERVED_PORTS = frozenset({80, 443, 8080, 8081, 8082})
VOLUME_LABEL = "mcps.companion"

_HOST = r"(?:localhost|[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"
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
    ports: tuple[int, ...] = ()
    env_keys: tuple[str, ...] = ()

    def to_meta(self) -> dict[str, object]:
        return {
            "companion_image": self.image,
            "companion_volumes": list(self.volumes),
            "companion_ports": list(self.ports),
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


def validate_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise CompanionConfigurationError("--companion-port must be 1-65535")
    if port in RESERVED_PORTS:
        raise CompanionConfigurationError(
            f"port {port} is used by the pod itself; pick the companion's own port"
        )
    return port


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
        key, separator, value = item.partition("=")
        if not separator:
            raise CompanionConfigurationError(
                "--companion-env expects KEY=VALUE, got a value without '='"
            )
        pairs[key] = value
    return pairs


def build(
    image: object,
    *,
    volumes: Sequence[object] = (),
    ports: Sequence[object] = (),
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
        ports=tuple(dict.fromkeys(validate_port(port) for port in ports)),
        env_keys=tuple(sorted(set(keys))),
    )


def from_meta(meta: Mapping[str, object]) -> Companion | None:
    """Saved settings are re-validated: metadata is a file anyone can edit."""
    if "companion_image" not in meta:
        return None
    lists: list[list[object]] = []
    for field in ("companion_volumes", "companion_ports", "companion_env_keys"):
        value = meta.get(field, [])
        if not isinstance(value, list):
            raise CompanionConfigurationError(
                "invalid saved companion configuration; re-run add with --companion"
            )
        lists.append(value)
    return build(
        meta["companion_image"], volumes=lists[0], ports=lists[1], env_keys=lists[2]
    )


def resolve(
    saved: Companion | None,
    *,
    image: str,
    volumes: Sequence[str],
    ports: Sequence[int],
    env_keys: Collection[str],
    remove: bool,
) -> Companion | None:
    """Combine a rebuild's flags with what the server already had.

    No flags keeps the companion. --companion redefines its image, volumes and
    ports from this command alone. Environment secrets persist either way.
    """
    if remove:
        if image or volumes or ports or env_keys:
            raise CompanionConfigurationError(
                "use --no-companion on its own, without other companion options"
            )
        return None
    kept = saved.env_keys if saved else ()
    if image:
        return build(image, volumes=volumes, ports=ports, env_keys={*kept, *env_keys})
    if volumes or ports:
        raise CompanionConfigurationError(
            "pass --companion IMAGE together with --companion-volume and "
            "--companion-port"
        )
    if saved is None:
        if env_keys:
            raise CompanionConfigurationError(
                "this server has no companion; pass --companion IMAGE"
            )
        return None
    return build(
        saved.image,
        volumes=saved.volumes,
        ports=saved.ports,
        env_keys={*kept, *env_keys},
    )


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


def running(server: str) -> bool:
    state = podman.run(
        "container",
        "inspect",
        f"{podman.pod_name(server)}-companion",
        "--format",
        "{{.State.Running}}",
        check=False,
    )
    return state == "true"


def remove_secrets(server: str, env_keys: Sequence[str]) -> None:
    for key in env_keys:
        podman.secret_rm(env_secret_name(server, key))


def remove_volumes(server: str) -> None:
    """Delete a removed server's companion data. Rebuilds never call this."""
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
    for name in listed.split():
        # Belt and braces: never trust the filter alone with another server's data.
        if name.rpartition("--")[0] == f"mcps-companion-{server}":
            podman.run("volume", "rm", "-f", name, check=False)
