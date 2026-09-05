"""Read optional source-IP policy from data, and normalize it for the gateway."""
import ipaddress
from pathlib import Path


def resolve(entries: list[str], path: Path | None) -> str:
    # Explicit CLI values override the default file, including --allow any.
    chosen = list(entries)
    if not chosen and path is not None:
        chosen = [line.split('#', 1)[0].strip() for line in path.read_text(encoding='utf-8-sig').splitlines()]
    chosen = [entry.strip() for entry in chosen if entry.strip()]
    if any(entry.lower() == 'any' for entry in chosen):
        if len(chosen) != 1:
            raise ValueError("'any' must be the only allowlist entry")
        return ''
    networks = []
    for entry in chosen:
        try:
            networks.append(str(ipaddress.ip_network(entry, strict=False)))
        except ValueError:
            raise ValueError(f"invalid allowlist entry: {entry}; use an IP or CIDR such as 192.0.2.0/24") from None
    return ','.join(dict.fromkeys(networks))
