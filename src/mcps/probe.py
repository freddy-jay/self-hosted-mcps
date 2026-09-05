"""End-to-end check: a real MCP initialize handshake over the tailnet URL."""
from __future__ import annotations

import json
import socket
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
    def __init__(self, message: str, *, status_code: int | None = None, dns_failure: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.dns_failure = dns_failure


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a 302 can copy Authorization to a different origin.
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _post(url: str, payload: dict, headers: dict[str, str], timeout: int = 25):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    return _open(request, timeout)


def _payload(body: str) -> dict:
    text = body
    for line in body.splitlines():
        if line.startswith("data:"):
            text = line[5:].strip()
            break
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ProbeError("unexpected non-JSON reply") from None
    if not isinstance(data, dict):
        raise ProbeError("expected a JSON-RPC object")
    return data


def _parse(body: str) -> str:
    data = _payload(body)
    if "error" in data:
        raise ProbeError("MCP initialize returned an error; inspect the server logs")
    result = data.get("result")
    info = result.get("serverInfo") if isinstance(result, dict) else None
    if data.get("id") != 1 or not isinstance(info, dict) or not isinstance(info.get("name"), str):
        raise ProbeError("invalid MCP initialize result")
    return " ".join(str(value) for value in [info["name"], info.get("version", "")] if value)


def initialize(url: str, token: str = "", attempts: int = 12, gap: float = 5.0) -> str:
    """Return the server's self-reported name, or raise ProbeError."""
    last = ProbeError("no response")
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
            with _open(request, timeout=20) as response:
                name = _parse(response.read().decode("utf-8", "replace"))
                session = response.headers.get("mcp-session-id", "")
            # initialize is answered by the bridge itself, so it succeeds even
            # when the MCP process died on startup - a missing API key being the
            # usual cause. Listing tools is the first call that needs the child.
            _list_tools(url, headers, session)
            return name
        except urllib.error.HTTPError as exc:
            last = ProbeError(f"HTTP {exc.code} {exc.reason}", status_code=exc.code)
            if exc.code in (401, 403):
                raise last from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last = ProbeError(str(reason), dns_failure=isinstance(reason, socket.gaierror))
        except ProbeError as exc:
            last = exc
        if attempt < attempts - 1:
            time.sleep(gap)
    raise last


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


def _list_tools(url: str, headers: dict[str, str], session: str) -> None:
    """Raise ProbeError unless the MCP process itself answers."""
    call_headers = dict(headers)
    if session:
        call_headers["mcp-session-id"] = session
    try:
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, call_headers).close()
        with _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, call_headers) as response:
            body = _payload(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"listing tools failed: HTTP {exc.code} {exc.reason}", status_code=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise ProbeError(f"listing tools failed: {reason}", dns_failure=isinstance(reason, socket.gaierror)) from None

    if "error" in body:
        raise ProbeError("the server accepted a connection but its tools are unavailable; inspect the server logs")
    result = body.get("result")
    if body.get("id") != 2 or not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise ProbeError("invalid MCP tools/list result")
