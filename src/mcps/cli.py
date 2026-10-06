"""mcps - self-host any MCP server in a Podman pod, reachable only over your tailnet."""
from __future__ import annotations

import json
import re
import os
import secrets
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from mcps import autostart as boot
from mcps import access, companions, config, detect, nodename, podman, probe, tailnet, tunnels, validation

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()
err = Console(stderr=True)

META_DIR = config.HOME / "servers"


def fail(message: str) -> None:
    err.print(f"[red]x[/red] {message}")
    raise typer.Exit(1)


def server_argument(value: str) -> str:
    """Report invalid names as normal CLI usage errors, before accessing Podman."""
    try:
        return validation.server_name(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


def meta_path(name: str) -> Path:
    validation.server_name(name)
    return META_DIR / f"{name}.json"


def write_meta(name: str, data: dict) -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    meta_path(name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_meta(name: str) -> dict:
    path = meta_path(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


BRIDGE_PORT = 8081   # supergateway, ungated
GATEWAY_PORT = 8080  # our gateway: bearer token, optional IP allowlist


def serve_config(https: bool, public: bool = False) -> str:
    """One listener on the standard port.

    Tailnet-only servers go straight to the bridge, since Tailscale ACLs already
    gate them. Public servers put the gateway behind Funnel on 443 - clients that
    reach a connector over the internet will not follow a non-standard port.
    """
    port, scheme = ("443", "HTTPS") if https else ("80", "HTTP")
    host = "${TS_CERT_DOMAIN}:" + port
    upstream = GATEWAY_PORT if public else BRIDGE_PORT
    return json.dumps({
        "TCP": {port: {scheme: True}},
        "Web": {host: {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{upstream}"}}}},
        "AllowFunnel": {host: public},
    })


def funnel_acl(cfg: dict) -> str:
    """The policy that lets a node use Funnel, scoped to a tag when one is set."""
    tags = re.findall(r"tag:[\w-]+", cfg.get("ts_extra_args", ""))
    if not tags:
        return json.dumps(
            {"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]}, indent=2
        )
    return json.dumps(
        {
            "tagOwners": {tag: ["autogroup:admin"] for tag in tags},
            "nodeAttrs": [{"target": tags, "attr": ["funnel"]}],
        },
        indent=2,
    )


def funnel_active(ts_container: str) -> bool:
    """Funnel is ACL-gated and fails silently, so confirm it rather than assume it."""
    result = subprocess.run(
        ["podman", "exec", ts_container, "tailscale", "funnel", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and "Funnel on" in result.stdout


def read_authkey() -> str:
    return podman.secret_get(config.AUTHKEY_SECRET)


def migrate_authkey(cfg: dict) -> None:
    """Older versions kept the key in config.json; move it into a podman secret."""
    stale = cfg.pop("authkey", "")
    if stale:
        podman.secret_set(config.AUTHKEY_SECRET, stale)
        config.save(cfg)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def server_url(cfg: dict, name: str, dns_name: str) -> str:
    scheme = "https" if cfg["https"] else "http"
    host = dns_name or f"{nodename.wanted_hostname(name)}.{cfg.get('tailnet', '')}"
    return f"{scheme}://{host}/mcp"


def local_healthy(app_container: str) -> bool:
    """Is the bridge answering inside the pod? Separates the app leg from the tailnet leg."""
    script = (
        f"fetch('http://127.0.0.1:{BRIDGE_PORT}/healthz')"
        ".then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
    )
    probe_run = subprocess.run(
        ["podman", "exec", app_container, "node", "-e", script], capture_output=True
    )
    return probe_run.returncode == 0


def report_broken(name: str, url: str, reason: probe.ProbeError, local_ok: bool, allow_cidrs: str = "") -> None:
    err.print(f"[red]x[/red] {name} started but did not answer an MCP handshake: {reason}")
    if allow_cidrs and reason.status_code == 403:
        err.print("  The IP allowlist blocked this machine's setup check.")
        err.print("  Check the source address in the gateway logs and update your allowlist file.")
        err.print("  Re-run add with --force to apply it. No extra IP ranges are allowed automatically.")
    elif local_ok:
        err.print(f"  The server is up inside the pod, so the tailnet leg failed for {url}.")
        err.print("  HTTPS certificates are a separate toggle from MagicDNS in the Tailscale admin panel.")
        err.print(f"  Enable them, or fall back to plain HTTP: mcps init --http && mcps add ... --force")
        if allow_cidrs:
            err.print("  An IP allowlist is set. If this machine is not on it and the server is silent,")
            err.print("  the setup check is dropped without an answer and looks exactly like this.")
    else:
        err.print("  The MCP process itself is not answering - usually a missing API key or a bad entrypoint.")
        err.print(f"  See: mcps logs {name}")
        err.print(f"  Then re-run add with -e KEY=VALUE or --cmd, plus --force.")
    err.print(f"  The pod is left running so you can inspect it. Remove it with: mcps rm {name}")
    if companions.volume_names(name):
        err.print("  mcps rm also deletes this server's companion data; add --keep-data to keep it.")


# The gateway owns these; letting a user secret target one would collide with it.
RESERVED_ENV = {"MCP_TOKEN", "MCP_ALLOW_CIDRS", "MCP_CMD", "MCP_PORT", "MCP_SILENT", "MCP_REQUIRE_TOKEN"}


def validate_env(key: str) -> None:
    try:
        validation.env_name(key)
    except ValueError as exc:
        fail(str(exc))
    if key in RESERVED_ENV:
        fail(f"{key} is set by mcps itself. Use the gateway options instead.")


def validate_env_keys(keys) -> None:
    seen: dict[str, str] = {}
    for key in keys:
        validate_env(key)
        slug = podman.env_secret_name("validation", key)
        if slug in seen and seen[slug] != key:
            fail(f"environment names {seen[slug]} and {key} map to the same Podman secret")
        seen[slug] = key


def store_env(name: str, pairs: dict[str, str], previous: dict) -> list[str]:
    """Each variable becomes its own podman secret, so no value lands on disk.

    Supplying no -e on a rebuild keeps the variables the server already had.
    """
    validate_env_keys(set(previous.get("env_keys", [])) | set(pairs))
    for key, value in pairs.items():
        podman.secret_set(podman.env_secret_name(name, key), value)
    # Anything already stored for this server - including secrets seeded with
    # `mcps secrets add` before the server existed - is carried over.
    kept = [k for k in previous.get("env_keys", []) if podman.secret_get(podman.env_secret_name(name, k))]
    return sorted((set(kept) | set(pairs)) - RESERVED_ENV)


def app_args(name: str, env_keys: list[str], has_token: bool, allow_cidrs: str, silent: bool = False) -> tuple[str, ...]:
    """Everything the app container needs beyond its image."""
    args: list[str] = ["--cap-drop=ALL", "--security-opt=no-new-privileges"]
    validate_env_keys(env_keys)
    for key in env_keys:
        validate_env(key)
        secret = podman.env_secret_name(name, key)
        if podman.secret_get(secret):
            args += ["--secret", f"{secret},type=env,target={key}"]
    if has_token:
        args += ["-e", "MCP_REQUIRE_TOKEN=1"]
        args += ["--secret", f"{podman.secret_name(name, 'token')},type=env,target=MCP_TOKEN"]
    if allow_cidrs:
        args += ["-e", f"MCP_ALLOW_CIDRS={allow_cidrs}"]
    if silent:
        args += ["-e", "MCP_SILENT=1"]
    return tuple(args)


def restart_app(name: str) -> None:
    """Recreate the app container from its stored metadata, picking up new secrets."""
    meta = read_meta(name)
    pod = podman.pod_name(name)
    args = app_args(name, list(meta.get("env_keys", [])), bool(meta.get("public")),
                    meta.get("allow_cidrs", ""), bool(meta.get("silent")))
    podman.run("rm", "-f", "-t", "3", f"{pod}-app", check=False)
    podman.run(
        "run", "-d", "--pod", pod, "--name", f"{pod}-app", "--restart", "always",
        *args,
        meta.get("image", f"localhost/mcps-{name}:latest"),
    )


def settle_node_name(name: str, container: str, *, dns_name: str, ip: str) -> tuple[str, str]:
    """Take mcp-<name> back if the node came up suffixed; return (dns_name, ip)."""
    if nodename.claim(name, container, dns_name) == dns_name:
        return dns_name, ip
    # The serve config and certificate are bound to the name at start-up.
    podman.run("restart", container)
    return wait_online(container)


def reclaim_failed(name: str, reason: str) -> None:
    """The server is online; a name reclaim that did not finish must not undo that."""
    if reason:
        err.print(f"[red]x[/red] {reason}")
    err.print(f"[red]x[/red] {name} is running, but taking its tailnet name back did not finish.")
    err.print(f"  See: mcps logs {name} --tailscale   then run: mcps restart {name}")
    raise typer.Exit(1)


def name_warning(name: str, dns_name: str) -> None:
    """Say so when the node is not on the name its clients expect."""
    wanted = nodename.wanted_hostname(name)
    # highlight=False: rich would otherwise colour the digits inside the name.
    if nodename.label(dns_name) == nodename.away_hostname(name):
        err.print(
            f"\n[yellow]![/yellow] this server's tailnet name is still settling back to {wanted},",
            highlight=False,
        )
        err.print("  so the URL above is temporary. Give it a moment, then run:")
        err.print(f"  mcps restart {name}")
    elif nodename.was_suffixed(name, dns_name):
        err.print(
            f"\n[yellow]![/yellow] this server is on your tailnet as {nodename.label(dns_name)}, not {wanted}:",
            highlight=False,
        )
        err.print("  another node still holds that name, so the URL above is not the usual one.")
        err.print("  Remove the stale node at login.tailscale.com/admin/machines, then run:")
        err.print(f"  mcps restart {name}")


def url_change_notice(old: str, new: str) -> None:
    if old and old != new:
        console.print(f"\n[yellow]![/yellow] URL changed from {old}", highlight=False)
        console.print(f"  to {new}. Update the clients that used the old one.", highlight=False)


def wait_online(container: str, timeout: int = 90) -> tuple[str, str]:
    """Block until tailscaled is up; return (dns_name, tailscale_ip)."""
    deadline = time.time() + timeout
    last = "starting"
    while time.time() < deadline:
        proc = subprocess.run(
            ["podman", "exec", container, "tailscale", "status", "--json"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            try:
                status = json.loads(proc.stdout)
            except json.JSONDecodeError:
                status = {}
            last = status.get("BackendState", last)
            if last == "Running":
                self_node = status.get("Self") or {}
                ips = self_node.get("TailscaleIPs") or [""]
                return self_node.get("DNSName", "").strip("."), ips[0]
            # NeedsLogin shows up briefly while tailscaled starts, so it only
            # means a bad key once the daemon publishes an interactive auth URL.
            if last == "NeedsLogin" and status.get("AuthURL"):
                fail(
                    "tailscale rejected the auth key. Generate a new reusable, "
                    "ephemeral, pre-approved key and run: mcps init"
                )
        exists = subprocess.run(
            ["podman", "container", "exists", container], capture_output=True
        )
        if exists.returncode != 0:
            fail(f"the tailscale container exited. See: podman logs {container}")
        time.sleep(2)
    fail(f"tailscale did not come online within {timeout}s (state: {last}). See: podman logs {container}")
    raise AssertionError  # unreachable


@app.command()
def init(
    authkey: str = typer.Option("", "--authkey", "-k", help="Tailscale auth key (reusable + ephemeral + pre-approved)."),
    https: bool | None = typer.Option(None, "--https/--http", help="Serve HTTPS on the tailnet (needs MagicDNS + HTTPS certs enabled). HTTPS unless you chose --http before."),
    tags: str = typer.Option("", "--tags", help="Extra tailscaled args, e.g. --advertise-tags=tag:mcp"),
    silent_default: bool | None = typer.Option(None, "--silent-default/--no-silent-default", help="Make new public servers drop unauthorised requests without answering."),
) -> None:
    """Store your Tailscale auth key and detect your tailnet."""
    try:
        podman.preflight()
    except podman.PodmanError as exc:
        fail(str(exc))

    cfg = config.load()
    migrate_authkey(cfg)
    authkey = authkey or os.environ.get("TS_AUTHKEY", "")
    stored = read_authkey()
    if not authkey and not stored:
        authkey = typer.prompt("Tailscale auth key (tailscale.com/admin/settings/keys)", hide_input=True)

    if authkey:
        if not authkey.startswith("tskey-"):
            fail("that does not look like a Tailscale auth key (expected it to start with 'tskey-').")
        podman.secret_set(config.AUTHKEY_SECRET, authkey.strip())
    if https is not None:
        cfg["https"] = https
    if tags:
        cfg["ts_extra_args"] = tags
    if silent_default is not None:
        cfg["silent"] = silent_default
    suffix = tailnet.magic_dns_suffix()
    if suffix:
        cfg["tailnet"] = suffix
    config.save(cfg)

    verb = "stored" if authkey else "kept"
    console.print(f"[green]+[/green] key {verb} as podman secret {config.AUTHKEY_SECRET}")
    if cfg.get("ts_extra_args"):
        console.print(f"  tailscaled args: {cfg['ts_extra_args']}")
    if cfg.get("silent"):
        console.print("  new public servers drop unauthorised requests silently")
    console.print(f"  settings: {config.CONFIG_PATH}")
    tail = cfg.get("tailnet") or "[yellow]unknown - install Tailscale on this machine[/yellow]"
    console.print(f"  tailnet: {tail}")


@app.command()
def add(
    target: str = typer.Argument(..., help="GitHub URL or owner/repo, or npm:<pkg>, or pypi:<pkg>."),
    *,
    name: str = typer.Option("", "--name", "-n", help="Name on your tailnet (default: repo name)."),
    cmd: str = typer.Option("", "--cmd", help="Override the stdio command the server is started with."),
    ref: str = typer.Option("", "--ref", help="Git branch or tag."),
    subdir: str = typer.Option("", "--subdir", help="Path inside the repo, for monorepos."),
    env: list[str] = typer.Option([], "--env", "-e", help="Environment variable KEY=VALUE, repeatable."),
    env_file: Path = typer.Option(None, "--env-file", exists=True, help="File of KEY=VALUE lines."),
    public: bool | None = typer.Option(None, "--public/--private", help="Public HTTPS or tailnet-only access. Rebuilds preserve existing access when omitted."),
    silent: bool | None = typer.Option(None, "--silent/--no-silent", rich_help_panel="Advanced", help="Drop unauthorised requests instead of answering 401. Rebuilds preserve this setting when omitted."),
    new_token: bool = typer.Option(False, "--new-token", rich_help_panel="Advanced", help="Rotate the bearer token instead of keeping the existing one."),
    token_stdin: bool = typer.Option(False, "--token-stdin", rich_help_panel="Advanced", help="Read the bearer token from stdin, e.g. from a password manager."),
    allow: list[str] = typer.Option([], "--allow", rich_help_panel="Advanced", help="Restrict source IPs (CIDR); repeatable. Usually unnecessary."),
    allow_file: Path = typer.Option(None, "--allow-file", exists=True, dir_okay=False, rich_help_panel="Advanced", help="IP list file. Defaults to ~/.mcps/allowlist.txt if present."),
    rebuild_base: bool = typer.Option(False, "--rebuild-base", rich_help_panel="Advanced", help="Rebuild the shared runtime image."),
    companion: str = typer.Option("", "--companion", rich_help_panel="Companion", help="Image to run beside the server in the same pod, e.g. ghcr.io/owner/image:tag. Rebuilds keep it when omitted."),
    companion_volume: list[str] = typer.Option([], "--companion-volume", rich_help_panel="Companion", help="Path inside the companion to keep in a persistent volume; repeatable."),
    companion_env: list[str] = typer.Option([], "--companion-env", rich_help_panel="Companion", help="Companion environment variable KEY=VALUE, stored as a secret; repeatable."),
    no_companion: bool = typer.Option(False, "--no-companion", rich_help_panel="Companion", help="Remove the companion and its secrets. Its volumes stay until mcps rm."),
    new_node: bool = typer.Option(False, "--new-node", rich_help_panel="Advanced", help="Register a new tailnet node on a rebuild instead of reusing the server's node."),
    force: bool = typer.Option(False, "--force", "-f", help="Replace an existing server with this name."),
) -> None:
    """Build an MCP server and put it on your tailnet."""
    try:
        podman.preflight()
    except podman.PodmanError as exc:
        fail(str(exc))

    cfg = config.load()
    migrate_authkey(cfg)
    if not read_authkey():
        fail("no Tailscale auth key yet. Run: mcps init")

    name = name or detect.default_name(target)
    try:
        validation.server_name(name)
        validation.workdir(subdir or ".")
        validation.source_path(config.SRC_DIR, name)
    except ValueError as exc:
        fail(str(exc))
    previous = read_meta(name)
    if previous.get("tunnel_id"):
        # Validate before fetching/building or removing the working pod.
        saved_id = previous["tunnel_id"]
        saved_image = previous.get("tunnel_image", "")
        try:
            if not isinstance(saved_id, str) or not isinstance(saved_image, str):
                raise tunnels.TunnelConfigurationError("invalid saved tunnel configuration")
            tunnels.validate_id(saved_id)
            tunnels.validate_image(saved_image)
            if not podman.secret_get(podman.secret_name(name, "openai-key")):
                raise tunnels.TunnelConfigurationError(
                    "the saved OpenAI tunnel has no runtime key. "
                    f"Run: mcps tunnel {name} --remove, then set the tunnel up again"
                )
        except tunnels.TunnelConfigurationError as exc:
            fail(str(exc))
    # Same rule for the companion: reject bad flags or saved settings up front.
    try:
        try:
            saved_companion = companions.from_meta(previous)
        except companions.CompanionConfigurationError:
            # Broken saved settings must not block replacing or removing them.
            if not (companion or no_companion):
                raise
            saved_companion = None
        companion_pairs = companions.parse_env(companion_env)
        sidecar = companions.resolve(
            saved_companion, image=companion, volumes=companion_volume,
            env_keys=set(companion_pairs), remove=no_companion,
        )
    except companions.CompanionConfigurationError as exc:
        fail(str(exc))
    public = bool(previous.get("public")) if public is None else public
    if silent is None:
        # A public server keeps what it had. A server that is not public yet
        # follows `mcps init --silent-default`, and silent mode it carried from
        # an earlier public life is never dropped on the way back.
        saved = bool(previous.get("silent"))
        silent = saved if previous.get("public") else saved or bool(cfg.get("silent", False))
    if public and not cfg["https"]:
        fail("--public needs HTTPS: Funnel is TLS-only on port 443. Run: mcps init --https")
    exists = podman.pod_exists(name)
    if exists and not force:
        fail(f"'{name}' already exists. Use --force to replace it, or mcps rm {name}.")

    # Keep the token stable across rebuilds so an already-configured client keeps working.
    token = ""
    if public:
        if token_stdin:
            token = sys.stdin.read().strip()
            if not token:
                fail("--token-stdin got nothing on stdin.")
        elif not new_token:
            token = podman.secret_get(podman.secret_name(name, "token"))
        token = token or secrets.token_urlsafe(32)  # 256 bits, nothing human types it
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            fail("bearer token must contain 32-256 URL-safe letters, digits, underscores or hyphens")

    # Opt in only where the proxy supplies a trustworthy source address.
    allow_cidrs = ""
    allow_source = ""
    if allow_file and allow:
        fail("use --allow-file or --allow, not both")
    if public:
        default_file = config.ALLOWLIST_PATH if config.ALLOWLIST_PATH.exists() else None
        # A flag-less rebuild keeps a policy the user chose with --allow or
        # --allow-file. One that came from the default file follows that file,
        # and a private server has no policy of its own to carry over.
        keep_saved = bool(previous.get("public")) and "allow_cidrs" in previous and (
            previous.get("allow_source") == "explicit" or default_file is None
        )
        try:
            if allow or allow_file:
                allow_cidrs = access.resolve(allow, allow_file)
                allow_source = "explicit"
            elif keep_saved:
                allow_cidrs = access.resolve(previous["allow_cidrs"].split(","), None)
                allow_source = previous.get("allow_source", "")
            else:
                allow_cidrs = access.resolve([], default_file)
                allow_source = "file" if default_file else ""
        except (ValueError, OSError) as exc:
            fail(str(exc))

    pairs: dict[str, str] = {}
    for item in env:
        if "=" not in item:
            fail(f"--env expects KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        validate_env(key)
        pairs[key] = value
    if env_file:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                validate_env(key)
                pairs.setdefault(key, value)
    validate_env_keys(set(previous.get("env_keys", [])) | set(pairs))

    # Before the build: a failed pull must not leave a new, never-run app image
    # tagged for the server that is still running the old one.
    if sidecar:
        console.print(f"[cyan]->[/cyan] companion image: [bold]{sidecar.image}[/bold]")
        try:
            companions.ensure_image(sidecar.image)
        except podman.PodmanError as exc:
            fail(str(exc))

    console.print(f"[cyan]->[/cyan] fetching {target}")
    try:
        source = detect.fetch(target, name, ref or None, subdir or None)
        install, run_cmd = detect.detect(source)
    except detect.DetectError as exc:
        fail(str(exc))
    if cmd:
        run_cmd = cmd
    console.print(f"[cyan]->[/cyan] command: [bold]{run_cmd}[/bold]")

    console.print("[cyan]->[/cyan] building image")
    try:
        podman.ensure_base_image(rebuild_base)
    except podman.PodmanError as exc:
        fail(str(exc))

    template = (podman.RUNTIME / "mcp.Containerfile.tmpl").read_text(encoding="utf-8")
    try:
        containerfile = validation.build_file(source.context)
    except ValueError as exc:
        fail(str(exc))
    containerfile.write_text(
        template.replace("__BASE_IMAGE__", podman.BASE_IMAGE)
        .replace("__WORKDIR__", source.workdir)
        .replace("__INSTALL__", install)
        .replace("__RUNCMD__", json.dumps(run_cmd)),
        encoding="utf-8",
    )
    image = f"localhost/mcps-{name}:latest"
    if podman.stream("build", "-t", image, "-f", str(containerfile), str(source.context)) != 0:
        fail("image build failed. Fix the build, or pass --cmd if the entrypoint was guessed wrong.")

    console.print("[cyan]->[/cyan] starting pod")
    # Keep the working server until input validation and image build succeed.
    # A deployed server is rebuilt as the same tailnet node, so its name and URL
    # survive - also across failed attempts, when the pod is already gone. The
    # node is replaced when tailscaled would carry over what must change - its
    # saved `tailscale up` flags, a Funnel config left from public access, the
    # login of another auth key - or when --new-node asks for it. A server built
    # before these were recorded is taken as unchanged.
    ts_extra_args = cfg.get("ts_extra_args", "")
    authkey_fingerprint = token_fingerprint(read_authkey())
    same_node = (
        not new_node
        and bool(previous.get("url"))
        and bool(previous.get("public")) == public
        and previous.get("ts_extra_args", ts_extra_args) == ts_extra_args
        and previous.get("authkey_fingerprint", authkey_fingerprint) == authkey_fingerprint
    )
    if exists or not same_node:
        # Clean up first: a pod, or state left behind by an earlier attempt.
        podman.destroy(name, keep_identity=same_node)
    volume = f"mcps-ts-{name}"
    pod = podman.pod_name(name)
    ts_container = f"{pod}-ts"
    try:
        podman.write_serve_config(volume, serve_config(cfg["https"], public))
        podman.run(
            "pod", "create", "--name", pod,
            "--label", f"mcps.name={name}",
            "--label", f"mcps.origin={source.origin}",
            "--label", f"mcps.cmd={run_cmd}",
            "--label", f"mcps.public={str(public).lower()}",
        )
        extra = ("-e", f"TS_EXTRA_ARGS={ts_extra_args}") if ts_extra_args else ()
        podman.run(
            "run", "-d", "--pod", pod, "--name", ts_container, "--restart", "always",
            "--secret", f"{config.AUTHKEY_SECRET},type=env,target=TS_AUTHKEY",
            "-e", f"TS_HOSTNAME={nodename.wanted_hostname(name)}",
            "-e", "TS_USERSPACE=true",
            "-e", "TS_STATE_DIR=/var/lib/tailscale",
            "-e", "TS_SERVE_CONFIG=/var/lib/tailscale/serve.json",
            *extra,
            "-v", f"{volume}:/var/lib/tailscale",
            podman.TS_IMAGE,
        )
        env_keys = store_env(name, pairs, previous)
        if token:
            podman.secret_set(podman.secret_name(name, "token"), token)
        podman.run(
            "run", "-d", "--pod", pod, "--name", f"{pod}-app", "--restart", "always",
            *app_args(name, env_keys, bool(token), allow_cidrs, silent),
            image,
        )
        if sidecar:
            for key, value in companion_pairs.items():
                podman.secret_set(companions.env_secret_name(name, key), value)
            sidecar = companions.with_stored_env(name, sidecar)
            companions.start(name, sidecar)
        if previous.get("tunnel_id"):
            tunnels.start(name, tunnel_id=previous["tunnel_id"], image=previous["tunnel_image"])
    except podman.PodmanError as exc:
        podman.destroy(name, keep_identity=same_node)
        fail(str(exc))

    try:
        dns_name, ip = wait_online(ts_container)
    except typer.Exit:
        podman.destroy(name, keep_identity=same_node)
        raise

    url = server_url(cfg, name, dns_name)
    exposed = url if public else ""
    write_meta(name, {
        "public_url": exposed,
        "name": name, "origin": source.origin, "cmd": run_cmd,
        "url": url, "ip": ip, "image": image, "public": public,
        "token_fingerprint": token_fingerprint(token) if token else "",
        "allow_cidrs": allow_cidrs, "allow_source": allow_source,
        "ts_extra_args": ts_extra_args, "authkey_fingerprint": authkey_fingerprint,
        "env_keys": env_keys, "silent": silent,
        **{k: previous[k] for k in ("tunnel_id", "tunnel_image") if k in previous},
        **(sidecar.to_meta() if sidecar else {}),
    })
    if saved_companion and not sidecar:
        companions.remove_secrets(name, saved_companion.env_keys)

    # The server is online and recorded. Only now try to take a suffixed name
    # back: if that fails, the pod keeps serving under the name it has.
    try:
        settled = settle_node_name(name, ts_container, dns_name=dns_name, ip=ip)
    except podman.PodmanError as exc:
        reclaim_failed(name, str(exc))
    except typer.Exit:
        reclaim_failed(name, "")
    if settled != (dns_name, ip):
        dns_name, ip = settled
        url = server_url(cfg, name, dns_name)
        exposed = url if public else ""
        write_meta(name, {**read_meta(name), "url": url, "public_url": exposed, "ip": ip})

    console.print("[cyan]->[/cyan] handshaking with the server")
    try:
        server_name = probe.initialize(url, token)
    except probe.ProbeError as exc:
        healthy = local_healthy(f"{pod}-app")
        # A newly registered node takes a while to reach this machine's MagicDNS
        # cache, and a tagged node stays invisible until an ACL grants access to
        # the tag. Both are local resolution problems, not a broken server.
        if healthy and exc.dns_failure:
            err.print(f"[yellow]![/yellow] {url} does not resolve from this machine yet.")
            err.print("  The server answers inside the pod, so this is DNS on your side:")
            err.print("  a new node takes a moment, and a tagged node needs an ACL that grants")
            err.print(f"  access to its tag, e.g. {{\"action\": \"accept\", \"src\": [\"autogroup:member\"], \"dst\": [\"tag:mcp:*\"]}}")
            server_name = "not reachable from this machine yet"
        else:
            report_broken(name, url, exc, healthy, allow_cidrs)
            if sidecar and not companions.stays_running(name, fresh=True):
                err.print(f"  Its companion container is not running. See: mcps logs {name} --companion")
            raise typer.Exit(1)

    if previous.get("tunnel_id") and not tunnels.wait_ready(name):
        fail(f"MCP server is running, but its OpenAI tunnel is not ready. See: mcps logs {name} --tunnel")
    # The MCP handshake passes without the companion, so check it separately.
    if sidecar and not companions.stays_running(name, fresh=True):
        fail(f"MCP server is running, but its companion container is not staying up. See: mcps logs {name} --companion")
    console.print(f"\n[green]+[/green] [bold]{name}[/bold] is live ({server_name})")
    show_endpoint(name, url, exposed, token, public, ts_container, allow_cidrs)
    url_change_notice(previous.get("url", ""), url)
    name_warning(name, dns_name)
    if sidecar:
        console.print(f"\n  companion: [bold]{sidecar.image}[/bold], on the pod's 127.0.0.1")


def show_endpoint(
    name: str, url: str, exposed: str, token: str,
    public: bool, ts_container: str, allow_cidrs: str = "",
) -> None:
    if not public:
        console.print(f"\n  on your tailnet:  [bold]{url}[/bold]")
        console.print(f"  claude mcp add --transport http {name} {url}")
        console.print(f"  OpenAI Codex config: mcps client {name}")
        console.print("\n  Any device on your tailnet can reach it, Claude Code and Claude Desktop included.")
        console.print("  claude.ai runs off your tailnet - re-run with --public to add a Funnel endpoint.")
        console.print(json.dumps({"mcpServers": {name: {"type": "http", "url": url}}}, indent=2))
        return

    hostname = exposed.split("://", 1)[-1].split("/", 1)[0]
    if not funnel_active(ts_container) or probe.resolves_publicly(hostname) is False:
        err.print(f"\n[yellow]![/yellow] {hostname} does not resolve on the public internet, so nothing")
        err.print("  outside your tailnet can reach it. Tailscale accepts the Funnel config locally and")
        err.print("  publishes DNS only once the tailnet policy grants the funnel attribute.")
        err.print(f"  Add this at login.tailscale.com/admin/acls, then: mcps restart {name}")
        err.print(funnel_acl(config.load()))
        return

    console.print(f"\n  on the public internet, with the bearer token:")
    console.print(f"    [bold]{url}[/bold]")
    console.print(f"  Retrieve your credential explicitly: mcps token {name}")
    console.print(f"  OpenAI Codex config: mcps client {name}")
    console.print(f"  OpenAI Responses API example: mcps client {name} --client openai")
    console.print(
        "\n  For claude.ai: add a custom connector on the public URL. Paste the bearer token into\n"
        "  the header field if your account has one, otherwise use the token-in-URL form."
    )


@app.command("ls")
def list_servers() -> None:
    """List your self-hosted MCP servers."""
    try:
        podman.preflight()
        pods = podman.list_pods()
    except podman.PodmanError as exc:
        fail(str(exc))

    if not pods:
        console.print("No MCP servers yet. Add one: [bold]mcps add owner/repo[/bold]")
        return

    table = Table(box=None, pad_edge=False)
    for column in ("NAME", "STATUS", "URL", "SOURCE"):
        table.add_column(column, overflow="fold")
    for pod in sorted(pods, key=lambda p: (p.get("Labels") or {})["mcps.name"]):
        labels = pod.get("Labels") or {}
        name = labels["mcps.name"]
        meta = read_meta(name)
        state = (pod.get("Status") or "?").lower()
        colour = "green" if state.startswith("running") else "yellow"
        table.add_row(name, f"[{colour}]{state}[/{colour}]", meta.get("url", "-"), labels.get("mcps.origin", "-"))
    console.print(table)


@app.command("rm")
def remove(
    name: str = typer.Argument(..., callback=server_argument, help="Server name."),
    keep_image: bool = typer.Option(False, "--keep-image", help="Leave the built image on disk."),
    keep_data: bool = typer.Option(False, "--keep-data", help="Leave companion volumes in place."),
) -> None:
    """Remove a server, its tailnet node and its data."""
    known = (
        podman.pod_exists(name)
        or meta_path(name).exists()
        or bool(podman.secret_get(podman.secret_name(name, "token")))
        or bool(podman.secret_names(f"mcps-{name}-env-"))
        or bool(companions.volume_names(name))
    )
    if not known:
        fail(f"no server named '{name}'. See: mcps ls")
    podman.destroy(name)
    if not keep_data:
        companions.remove_volumes(name)
    if not keep_image:
        podman.run("rmi", "-f", f"localhost/mcps-{name}:latest", check=False)
    podman.secret_rm(podman.secret_name(name, "token"))
    podman.secret_rm(podman.secret_name(name, "openai-key"))
    for secret in podman.secret_names(f"mcps-{name}-env-"):
        podman.secret_rm(secret)
    meta_path(name).unlink(missing_ok=True)
    source_dir = validation.source_path(config.SRC_DIR, name)
    if source_dir.exists():
        detect.force_rmtree(source_dir)
    console.print(f"[green]+[/green] removed {name}")


@app.command()
def logs(
    name: str = typer.Argument(..., callback=server_argument, help="Server name."),
    *,
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new output."),
    tailscale: bool = typer.Option(False, "--tailscale", help="Show the tailscale sidecar instead."),
    tunnel: bool = typer.Option(False, "--tunnel", help="Show the OpenAI tunnel sidecar instead."),
    companion: bool = typer.Option(False, "--companion", help="Show the companion container instead."),
) -> None:
    """Show a server's logs."""
    if not podman.pod_exists(name):
        fail(f"no server named '{name}'. See: mcps ls")
    if tailscale + tunnel + companion > 1:
        fail("use only one of --tailscale, --tunnel or --companion")
    suffix = "tunnel" if tunnel else "ts" if tailscale else "companion" if companion else "app"
    container = f"{podman.pod_name(name)}-{suffix}"
    follow_args = ["-f"] if follow else []
    raise typer.Exit(podman.stream("logs", *follow_args, "--tail", "200", container))


@app.command()
def restart(name: str = typer.Argument(..., callback=server_argument, help="Server name.")) -> None:
    """Restart a server."""
    if not podman.pod_exists(name):
        fail(f"no server named '{name}'. See: mcps ls")
    pod = podman.pod_name(name)
    try:
        podman.run("pod", "restart", pod)
        dns_name, ip = wait_online(f"{pod}-ts")
        dns_name, ip = settle_node_name(name, f"{pod}-ts", dns_name=dns_name, ip=ip)
    except podman.PodmanError as exc:
        fail(str(exc))

    meta = read_meta(name)
    url = server_url(config.load(), name, dns_name)
    previous_url = meta.get("url", "")
    # public_url is the URL of a public server and nothing else; deriving it
    # also repairs metadata where an older version let the two drift apart.
    current = {"url": url, "public_url": url if meta.get("public") else ""}
    if any(meta.get(key) != value for key, value in current.items()):
        meta.update(current, ip=ip)
        write_meta(name, meta)

    if meta.get("tunnel_id") and not tunnels.wait_ready(name):
        fail(f"MCP server restarted, but its OpenAI tunnel is not ready. See: mcps logs {name} --tunnel")
    try:
        has_companion = companions.from_meta(meta) is not None
    except companions.CompanionConfigurationError as exc:
        fail(str(exc))
    if has_companion and not companions.stays_running(name, fresh=False):
        fail(f"MCP server restarted, but its companion container is not staying up. See: mcps logs {name} --companion")
    console.print(f"[green]+[/green] {name} is back")
    show_endpoint(
        name, url, meta.get("public_url", ""),
        podman.secret_get(podman.secret_name(name, "token")),
        bool(meta.get("public")), f"{pod}-ts", meta.get("allow_cidrs", ""),
    )
    url_change_notice(previous_url, url)
    name_warning(name, dns_name)


@app.command()
def token(
    name: str = typer.Argument(..., callback=server_argument, help="Server name."),
    rotate: bool = typer.Option(False, "--rotate", help="Replace the token with a fresh one."),
) -> None:
    """Show, or rotate, a public server's bearer token."""
    if not podman.pod_exists(name):
        fail(f"no server named '{name}'. See: mcps ls")
    secret = podman.secret_name(name, "token")
    meta = read_meta(name)
    if not meta.get("public"):
        fail("this is a tailnet-only server; it does not use a bearer token")
    if rotate:
        fresh = secrets.token_urlsafe(32)
        podman.secret_set(secret, fresh)
        meta["token_fingerprint"] = token_fingerprint(fresh)
        write_meta(name, meta)
        restart_app(name)
        console.print("[green]+[/green] rotated and applied. Retrieve it with: mcps token " + name)
        return

    current = podman.secret_get(secret)
    if not current:
        fail(f"{name} has no token - it is a tailnet-only server. Re-add it with --public to get one.")
    typer.echo(current)


secrets_app = typer.Typer(no_args_is_help=True, help="Manage a server's environment secrets.")
app.add_typer(secrets_app, name="secrets")


def require_server(name: str) -> dict:
    if not (podman.pod_exists(name) or meta_path(name).exists()):
        fail(f"no server named '{name}'. See: mcps ls")
    return read_meta(name)


@app.command("client")
def client_config(
    name: str = typer.Argument(..., callback=server_argument, help="Server name."),
    client: str = typer.Option("codex", "--client", help="codex (default) or openai (API example)."),
) -> None:
    """Get client setup. Run: mcps client NAME (Codex) or add --client openai."""
    from .clients import render
    meta = require_server(name)
    try:
        typer.echo(render(name, meta, client))
    except ValueError as exc:
        fail(str(exc))


@app.command("tunnel")
def tunnel_setup(
    name: str = typer.Argument(..., callback=server_argument, help="Server to connect privately to OpenAI."),
    *,
    tunnel_id: str = typer.Option("", "--tunnel-id", help="OpenAI tunnel ID. Omit for guided setup."),
    key_stdin: bool = typer.Option(False, "--key-stdin", help="Read the runtime API key from stdin instead of a hidden prompt."),
    status: bool = typer.Option(False, "--status", help="Check the existing tunnel's local readiness."),
    remove: bool = typer.Option(False, "--remove", help="Remove the local sidecar and key; preserve existing access."),
) -> None:
    """Add OpenAI Secure MCP Tunnel without changing existing client access.

    Setup preserves public Funnel and private Tailscale access. Create the tunnel in your
    OpenAI organization and associate your ChatGPT workspace when prompted.
    """
    if (status and remove) or ((status or remove) and (tunnel_id or key_stdin)):
        fail("use setup, --status, or --remove separately")
    if key_stdin and not tunnel_id:
        # The ID prompt would read the piped key as the tunnel ID.
        fail("--key-stdin needs --tunnel-id, because stdin carries the key")
    try:
        podman.preflight()
        meta = require_server(name)
        if remove:
            # Works without a pod: a rebuild that failed on a broken tunnel
            # has none, and removing the tunnel is the way out.
            tunnels.remove(name)
            for key in ("tunnel_id", "tunnel_image"):
                meta.pop(key, None)
            write_meta(name, meta)
            console.print("Removed the local tunnel. OpenAI tunnel records can be deleted at " + tunnels.SETTINGS_URL)
            return
        if not podman.pod_exists(name):
            fail("start or rebuild this server before configuring its tunnel")
        if status:
            if not meta.get("tunnel_id"):
                fail("no OpenAI tunnel configured; run mcps tunnel " + name)
            typer.echo("Tunnel: " + meta["tunnel_id"])
            if not tunnels.ready(name):
                fail("tunnel is not locally ready; see mcps logs " + name + " --tunnel")
            typer.echo("Tunnel ready; recent successful OpenAI poll verified. Confirm tool calls in your OpenAI client.")
            return
        if meta.get("tunnel_id"):
            fail("a tunnel is already configured; use --status, or --remove before replacing it")
        if not tunnel_id:
            console.print("Create a tunnel and associate your Platform organization and target ChatGPT workspace:")
            typer.echo(tunnels.SETTINGS_URL)
            console.print("Tunnel managers need Read + Manage; the runtime key owner needs Read + Use.")
            typer.launch(tunnels.SETTINGS_URL)
            tunnel_id = typer.prompt("Tunnel ID").strip()
        tunnels.validate_id(tunnel_id)
        if key_stdin:
            key = sys.stdin.read().strip()
        else:
            key = podman.secret_get(podman.secret_name(name, "openai-key"))
            if not key:
                console.print("Create a runtime API key (not an admin key); restrict it to Tunnels Read + Use:")
                typer.echo(tunnels.KEYS_URL)
                key = typer.prompt("Runtime API key (stored only as a Podman secret)", hide_input=True).strip()
        if not key or any(c.isspace() for c in key):
            fail("a non-empty runtime API key without whitespace is required")
        # Stored first, so a retry after a failed download reuses it.
        podman.secret_set(podman.secret_name(name, "openai-key"), key)
        del key
        console.print("Downloading and pinning the official OpenAI tunnel image...")
        image = tunnels.pull_image()
        meta.update(tunnel_id=tunnel_id, tunnel_image=image)
        tunnels.start(name, tunnel_id=tunnel_id, image=image)
        # Save immediately so interrupted setup can be inspected or removed.
        write_meta(name, meta)
        console.print("Checking readiness and waiting for a successful OpenAI poll...")
        if not tunnels.wait_ready(name):
            fail("tunnel not ready; public/Tailscale settings are unchanged. See mcps logs " + name + " --tunnel, then --remove to retry")
        # Adding a client transport must not remove another client's transport.
        # The tunnel uses pod loopback independently of Tailscale/Funnel routing.
        console.print("OpenAI tunnel configured. Existing Tailscale/Funnel access and authentication are unchanged.")
        typer.echo("Tailscale: " + meta.get("url", ""))
        typer.echo("OpenAI tunnel: " + tunnel_id)
        console.print("In ChatGPT, create a developer-mode app, choose Tunnel, and select this tunnel.")
        console.print("Readiness and OpenAI polling passed; verify a tool call from your OpenAI client.")
    except (podman.PodmanError, ValueError) as exc:
        fail(str(exc))


@secrets_app.command("add")
def secrets_add(
    server: str = typer.Argument(..., callback=server_argument, help="Server the variable belongs to."),
    name: str = typer.Option(..., "--name", "-n", help="Variable name, e.g. BRING_PASSWORD."),
    value: str = typer.Option("", "--value", "-v", help="Value. Omit it to be prompted instead, which keeps it out of your shell history."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the value from stdin, e.g. from a password manager."),
) -> None:
    """Add or replace one environment secret. Works before the server exists."""
    validate_env(name)
    meta = read_meta(server)
    validate_env_keys(set(meta.get("env_keys", [])) | {name})
    if stdin:
        value = sys.stdin.read().strip()
    elif not value:
        value = typer.prompt(f"Value for {name}", hide_input=True)
    if not value:
        fail("no value given.")

    podman.secret_set(podman.env_secret_name(server, name), value)
    meta.setdefault("name", server)
    meta["env_keys"] = sorted(set(meta.get("env_keys", [])) | {name})
    write_meta(server, meta)

    if podman.pod_exists(server):
        restart_app(server)
        console.print(f"[green]+[/green] {name} set on {server}, container recreated")
    else:
        console.print(f"[green]+[/green] {name} stored for {server}")
        console.print(f"  It is picked up when you create it: mcps add <target> --name {server}")


@secrets_app.command("ls")
def secrets_ls(server: str = typer.Argument(..., callback=server_argument, help="Server name.")) -> None:
    """List a server's environment secrets. Names only - values are never printed."""
    meta = require_server(server)
    keys = [k for k in meta.get("env_keys", []) if podman.secret_get(podman.env_secret_name(server, k))]
    if not keys:
        console.print(f"{server} has no environment secrets. Add one: mcps secrets add {server} --name KEY")
        return
    for key in keys:
        console.print(f"{key}  [dim]{podman.env_secret_name(server, key)}[/dim]")


@secrets_app.command("rm")
def secrets_rm(
    server: str = typer.Argument(..., callback=server_argument, help="Server name."),
    name: str = typer.Option(..., "--name", "-n", help="Variable name to remove."),
) -> None:
    """Remove one environment secret, then restart the server."""
    meta = require_server(server)
    if name not in meta.get("env_keys", []):
        fail(f"{server} has no secret named {name}. See: mcps secrets ls {server}")

    podman.secret_rm(podman.env_secret_name(server, name))
    meta["env_keys"] = [k for k in meta["env_keys"] if k != name]
    write_meta(server, meta)

    if podman.pod_exists(server):
        restart_app(server)
    console.print(f"[green]+[/green] {name} removed from {server}")


@app.command()
def autostart(
    enable: bool = typer.Option(None, "--enable/--disable", help="Turn boot persistence on or off. Omit to show the current state."),
) -> None:
    """Bring your servers back after a reboot. Off unless you turn it on."""
    try:
        podman.preflight()
    except podman.PodmanError as exc:
        fail(str(exc))

    if enable is None:
        for label, on in (("containers", boot.containers_enabled()), ("podman machine", boot.machine_enabled())):
            mark, colour = ("+", "green") if on else ("-", "yellow")
            console.print(f"[{colour}]{mark}[/{colour}] {label}: {'starts at boot' if on else 'does not start at boot'}")
        if not (boot.containers_enabled() and boot.machine_enabled()):
            console.print("\n  Turn it on with: mcps autostart --enable")
        return

    try:
        boot.set_containers(enable)
        note = boot.set_machine(enable)
    except (subprocess.CalledProcessError, podman.PodmanError) as exc:
        fail(f"could not change autostart: {exc}")

    word = "enabled" if enable else "disabled"
    console.print(f"[green]+[/green] autostart {word}")
    console.print(f"  containers: podman-restart.service {word} inside the machine")
    if note:
        console.print(f"  machine: {'wrote' if enable else 'removed'} {note}")


@app.command()
def doctor() -> None:
    """Check that everything this tool needs is in place."""
    ok = True

    def line(label: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        mark = "+" if good else "x"
        colour = "green" if good else "red"
        console.print(f"[{colour}]{mark}[/{colour}] {label}: {detail}")

    line("git", shutil.which("git") is not None, shutil.which("git") or "not on PATH")
    try:
        podman.preflight()
        line("podman", True, podman.run("version", "--format", "{{.Client.Version}}"))
    except podman.PodmanError as exc:
        line("podman", False, str(exc).splitlines()[0])
    suffix = tailnet.magic_dns_suffix()
    line("tailscale", suffix is not None, suffix or "host client not running (only needed for nicer URLs)")
    cfg = config.load()
    migrate_authkey(cfg)
    stored = bool(read_authkey())
    line("auth key", stored, f"podman secret {config.AUTHKEY_SECRET}" if stored else "missing - run: mcps init")
    raise typer.Exit(0 if ok else 1)
