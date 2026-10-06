"""A server's name on the tailnet, and taking it back after a clash.

When a node registers while another one still holds mcp-<name>, the control
plane names it mcp-<name>-1. It only picks a name again when the hostname
changes, so the way back is to rename away and then back once the holder is gone.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable

# A DNS label is at most 63 characters, and a server name may already fill it.
MAX_LABEL = 63


def wanted_hostname(name: str) -> str:
    return f"mcp-{name}"


def away_hostname(name: str) -> str:
    """A throwaway hostname that differs from the wanted one and fits a label."""
    return ("r-" + wanted_hostname(name))[:MAX_LABEL].rstrip("-")


def label(dns_name: str) -> str:
    return dns_name.split(".", 1)[0]


def was_suffixed(name: str, dns_name: str) -> bool:
    """True for mcp-<name>-<n>, the control plane's mark for a name that was taken.

    Any other name is left alone: a machine renamed by hand in the admin console
    is not a clash, and renaming it would not work anyway.
    """
    pattern = rf"{re.escape(wanted_hostname(name))}-\d+"
    return re.fullmatch(pattern, label(dns_name)) is not None


def current(container: str) -> str:
    """The node's DNS name as its sidecar reports it, or "" when it cannot say."""
    try:
        proc = subprocess.run(
            ["podman", "exec", container, "tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    try:
        status: object = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    node = status.get("Self") if isinstance(status, dict) else None
    dns_name = node.get("DNSName") if isinstance(node, dict) else None
    return dns_name.strip(".") if isinstance(dns_name, str) else ""


def _set_hostname(container: str, hostname: str) -> bool:
    try:
        proc = subprocess.run(
            ["podman", "exec", container, "tailscale", "set", f"--hostname={hostname}"],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _wait_for(container: str, accept: Callable[[str], bool], timeout: float) -> str:
    """Poll until the node's label is accepted; return the last name seen."""
    deadline = time.monotonic() + timeout
    while True:
        seen = current(container)
        if seen and accept(label(seen)):
            return seen
        if time.monotonic() >= deadline:
            return seen
        time.sleep(2)


def claim(name: str, container: str, dns_name: str, *, timeout: float = 20) -> str:
    """Try to move a suffixed node back to mcp-<name>; return the name it ends on.

    The result is mcp-<name> on success, the suffixed name if another node
    still holds it, and unchanged input for a node that was never suffixed.
    """
    if not was_suffixed(name, dns_name):
        return dns_name
    away, wanted = away_hostname(name), wanted_hostname(name)
    sent = _set_hostname(container, away)
    # `tailscale set` returns before the control plane has acted on it.
    landed = (
        sent and label(_wait_for(container, lambda seen: seen == away, timeout)) == away
    )
    # Ask for the real hostname again whatever happened above, so a failure
    # half way never leaves the node on the throwaway name.
    _set_hostname(container, wanted)
    if not sent:
        return current(container) or dns_name
    # Once the throwaway name was seen, any other name is the control plane's
    # answer. If it never showed, both renames may still be on their way, so
    # only the wanted name counts as an answer before the deadline.
    settled = _wait_for(
        container, lambda seen: seen == wanted or (landed and seen != away), timeout
    )
    return settled or dns_name
