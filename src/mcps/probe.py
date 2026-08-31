"""End-to-end check: a real MCP initialize handshake over the tailnet URL."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcps", "version": "0.1.0"},
    },
}


class ProbeError(RuntimeError):
    pass


def _parse(body: str) -> str:
    payload = body
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            break
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise ProbeError(f"unexpected reply: {body[:200]}") from None
    if "error" in data:
        raise ProbeError(str(data["error"].get("message", data["error"])))
    info = (data.get("result") or {}).get("serverInfo") or {}
    return " ".join(filter(None, [info.get("name", "server"), info.get("version", "")]))


def initialize(url: str, token: str = "", attempts: int = 12, gap: float = 5.0) -> str:
    """Return the server's self-reported name, or raise ProbeError."""
    last = "no response"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(INITIALIZE).encode(),
        headers=headers,
        method="POST",
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return _parse(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        except ProbeError as exc:
            last = str(exc)
        if attempt < attempts - 1:
            time.sleep(gap)
    raise ProbeError(last)


def resolves_publicly(hostname: str) -> bool | None:
    """Does the name resolve on the public internet?

    Tailscale accepts an AllowFunnel config locally and silently declines to
    publish DNS when the tailnet's ACL lacks the `funnel` attribute, so
    `tailscale funnel status` says "Funnel on" for a node the internet cannot
    find. This asks a public resolver over HTTPS - a plain lookup from a machine
    on the tailnet is answered by MagicDNS and never leaves the host.

    Returns None when the check itself could not be made.
    """
    for record in ("A", "AAAA"):
        url = f"https://dns.google/resolve?name={hostname}&type={record}"
        request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        if answer.get("Answer"):
            return True
    return False
