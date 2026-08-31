"""Optional boot persistence.

Two independent pieces have to be in place, and only the first exists by default:

  containers  every pod runs with --restart always, but rootless podman only
              replays that at boot if the *user* podman-restart unit is enabled
              inside the machine. It ships disabled.
  the machine on Windows and macOS the podman VM itself does not start at login,
              so nothing can come back until it does.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from . import podman

# `podman machine ssh` lands without a user session, and the machine image ships
# ~/.config/systemd owned by root, so both have to be fixed before systemctl --user works.
USER_CTL = "sudo chown -R $(id -u):$(id -g) ~/.config/systemd 2>/dev/null; XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user"

STARTUP_ENTRY = "mcps-podman-machine.vbs"
UNIT_NAME = "mcps-podman-machine.service"
RESTART_UNIT = "podman-restart.service"


# --- containers, inside the podman machine ---------------------------------


def containers_enabled() -> bool:
    if not podman.machine_exists():
        return _systemd_user_enabled(RESTART_UNIT)
    state = podman.machine_ssh(f"{USER_CTL} is-enabled {RESTART_UNIT}", check=False)
    return state.splitlines()[-1].strip() == "enabled" if state else False


def set_containers(enable: bool) -> None:
    action = "enable --now" if enable else "disable"
    if podman.machine_exists():
        podman.machine_ssh(f"{USER_CTL} {action} {RESTART_UNIT}")
    else:
        subprocess.run(["systemctl", "--user", *action.split(), RESTART_UNIT], capture_output=True)


# --- the machine, on the host ----------------------------------------------


def _systemd_user_enabled(unit: str) -> bool:
    proc = subprocess.run(["systemctl", "--user", "is-enabled", unit], capture_output=True, text=True)
    return proc.stdout.strip() == "enabled"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def machine_enabled() -> bool:
    if not podman.machine_exists():
        return True  # nothing to start
    if platform.system() == "Windows":
        return _startup_script().exists()
    if platform.system() == "Linux":
        return _systemd_user_enabled(UNIT_NAME)
    return _launch_agent().exists()


def _startup_script() -> Path:
    """A logon-scoped Startup entry, because a scheduled ONLOGON task needs admin."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_ENTRY


def _launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.mcps.podman-machine.plist"


def set_machine(enable: bool) -> str:
    """Returns a note for the user, or '' when nothing needed saying."""
    if not podman.machine_exists():
        return ""

    binary = podman.cli_path()
    system = platform.system()

    if system == "Windows":
        script = _startup_script()
        if enable:
            script.parent.mkdir(parents=True, exist_ok=True)
            # WScript.Shell with a window style of 0 keeps the console off screen.
            script.write_text(
                'CreateObject("WScript.Shell").Run '
                f'"""{binary}"" machine start", 0, False\n',
                encoding="utf-8",
            )
        else:
            script.unlink(missing_ok=True)
        return f"{script}"

    if system == "Linux":
        unit = _unit_path()
        if enable:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(
                "[Unit]\nDescription=Start the podman machine for mcps\n\n"
                f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={binary} machine start\n\n"
                "[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", UNIT_NAME], capture_output=True)
            unit.unlink(missing_ok=True)
        return f"systemd user unit {UNIT_NAME}"

    plist = _launch_agent()
    if enable:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            "  <key>Label</key><string>com.mcps.podman-machine</string>\n"
            "  <key>ProgramArguments</key>"
            f"<array><string>{binary}</string><string>machine</string><string>start</string></array>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "</dict></plist>\n",
            encoding="utf-8",
        )
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
    else:
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
    return f"launch agent {plist.name}"
