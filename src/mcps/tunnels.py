"""OpenAI's outbound tunnel sidecar; the MCP listener stays inside the pod."""

import json
import re
import time

from mcps import podman

SETTINGS_URL = "https://platform.openai.com/settings/organization/tunnels"
KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
IMAGE = "ghcr.io/openai/tunnel-client:latest"


def validate_id(value: str) -> str:
    if not re.fullmatch(r"tunnel_[a-f0-9]{32}", value):
        raise ValueError(
            "invalid tunnel ID; copy the tunnel_... value from OpenAI tunnel settings"
        )
    return value


def pull_image() -> str:
    podman.run("pull", IMAGE)
    digest = podman.run(
        "image", "inspect", IMAGE, "--format", "{{index .RepoDigests 0}}"
    )
    if not re.fullmatch(r"ghcr.io/openai/tunnel-client@sha256:[a-f0-9]{64}", digest):
        raise ValueError("could not pin the official tunnel image digest")
    return digest


def start(name: str, *, tunnel_id: str, image: str) -> None:
    identity = validate_id(tunnel_id)
    if not re.fullmatch(r"ghcr.io/openai/tunnel-client@sha256:[a-f0-9]{64}", image):
        raise ValueError("invalid saved tunnel image digest; run mcps tunnel again")
    pod = podman.pod_name(name)
    podman.run(
        "run",
        "-d",
        "--pod",
        pod,
        "--name",
        f"{pod}-tunnel",
        "--restart",
        "always",
        "--secret",
        f"{podman.secret_name(name, 'openai-key')},type=env,target=CONTROL_PLANE_API_KEY",
        "-e",
        f"CONTROL_PLANE_TUNNEL_ID={identity}",
        "-e",
        "MCP_SERVER_URL=http://127.0.0.1:8081/mcp",
        "-e",
        "MCP_STARTUP_WAIT_TIMEOUT=60s",
        "-e",
        "HEALTH_LISTEN_ADDR=127.0.0.1:8082",
        "-e",
        "LOG_FORMAT=json",
        "-e",
        "LOG_LEVEL=info",
        image,
    )


def ready(name: str) -> bool:
    # Probe from the app, which shares loopback with the sidecar. Publish no ports.
    script = (
        'Promise.all(["readyz","metrics"].map(async p=>{'
        'const r=await fetch("http://127.0.0.1:8082/"+p,{signal:AbortSignal.timeout(2000)});'
        'return p==="readyz"?r.status:await r.text()}))'
        '.then(v=>console.log(JSON.stringify(v))).catch(()=>console.log("[]"))'
    )
    raw = podman.run(
        "exec", f"{podman.pod_name(name)}-app", "node", "-e", script, check=False
    )
    try:
        result: object = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(result, list) or len(result) != 2:
        return False
    status, metrics = result
    return status == 200 and isinstance(metrics, str) and poll_is_recent(metrics)


def poll_is_recent(metrics: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    for value in re.findall(
        r"^commands_poll_last_successful_timestamp_seconds(?:\{[^\n]*\})?\s+([\d.eE+-]+)$",
        metrics,
        re.MULTILINE,
    ):
        try:
            if 0 <= now - float(value) <= 120:
                return True
        except ValueError:
            pass
    return False


def wait_ready(name: str, timeout: float = 45) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready(name):
            return True
        time.sleep(1)
    return False


def remove(name: str) -> None:
    podman.run("rm", "-f", "--ignore", f"{podman.pod_name(name)}-tunnel")
    podman.secret_rm(podman.secret_name(name, "openai-key"))
