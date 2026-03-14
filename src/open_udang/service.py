"""Install/uninstall OpenUdang as a system service (systemd or launchd)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from open_udang.config import DEFAULT_CONFIG_PATH

logger = logging.getLogger("open_udang")

# Service file locations
_SYSTEMD_UNIT_PATH = (
    Path.home() / ".config" / "systemd" / "user" / "open-udang.service"
)
_LAUNCHD_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / "com.openudang.bot.plist"
)
_LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "OpenUdang"
_LAUNCHD_LABEL = "com.openudang.bot"


def _detect_platform() -> str:
    """Return 'linux' or 'macos'.

    Raises:
        RuntimeError: On unsupported platforms.
    """
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    raise RuntimeError(
        f"Unsupported platform: {sys.platform}. "
        "Only Linux (systemd) and macOS (launchd) are supported."
    )


def _detect_executable() -> str:
    """Find the best executable path for the service.

    Returns the absolute path to the ``openudang`` binary or script.
    Falls back to ``sys.executable -m open_udang`` if the script is not
    found on PATH.
    """
    # 1. Check if openudang is on PATH
    which = shutil.which("openudang")
    if which:
        return str(Path(which).resolve())

    # 2. Check for the script next to the running Python interpreter
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / "openudang"
    if candidate.is_file():
        return str(candidate.resolve())

    # 3. Fallback: run as a module
    return f"{sys.executable} -m open_udang"


def _generate_systemd_unit(
    exec_path: str,
    config_path: str,
) -> str:
    """Generate a systemd user unit file."""
    return dedent(f"""\
        [Unit]
        Description=OpenUdang Telegram Bot
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart={exec_path} --config {config_path}
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
    """)


def _generate_launchd_plist(
    exec_path: str,
    config_path: str,
) -> str:
    """Generate a launchd user agent plist."""
    # Build ProgramArguments — handle the "python -m" fallback case
    parts = exec_path.split()
    args_xml = "\n".join(f"            <string>{p}</string>" for p in parts)
    args_xml += "\n            <string>--config</string>"
    args_xml += f"\n            <string>{config_path}</string>"

    log_dir = _LAUNCHD_LOG_DIR

    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
        {args_xml}
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{log_dir}/openudang.stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{log_dir}/openudang.stderr.log</string>
        </dict>
        </plist>
    """)


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output."""
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _service_path(platform: str) -> Path:
    """Return the service file path for the given platform."""
    if platform == "linux":
        return _SYSTEMD_UNIT_PATH
    return _LAUNCHD_PLIST_PATH


def install_service(config_path: str) -> None:
    """Install OpenUdang as a system service.

    On Linux, installs a systemd user unit and enables it.
    On macOS, installs a launchd user agent and loads it.

    Args:
        config_path: Path to the OpenUdang config file.
    """
    platform = _detect_platform()
    svc_path = _service_path(platform)

    # Check for existing installation
    if svc_path.exists():
        if sys.stdin.isatty():
            print(f"Service file already exists: {svc_path}")
            try:
                answer = input("Overwrite? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nInstall cancelled.")
                return
            if answer not in ("y", "yes"):
                print("Install cancelled.")
                return
            # Stop existing service before overwriting
            if platform == "linux":
                _run(
                    ["systemctl", "--user", "stop", "open-udang.service"],
                    check=False,
                )
            else:
                _run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}", str(svc_path)],
                    check=False,
                )
        else:
            print(f"Service file already exists: {svc_path}", file=sys.stderr)
            print("Run interactively to overwrite.", file=sys.stderr)
            sys.exit(1)

    # Resolve config path
    resolved_config = str(Path(config_path).expanduser().resolve())
    if not Path(resolved_config).exists():
        print(f"Warning: config file does not exist yet: {resolved_config}")
        print("Run 'openudang' first to complete the setup wizard.\n")

    # Detect executable
    exec_path = _detect_executable()

    # Generate and write service file
    if platform == "linux":
        content = _generate_systemd_unit(exec_path, resolved_config)
    else:
        content = _generate_launchd_plist(exec_path, resolved_config)
        _LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)

    svc_path.parent.mkdir(parents=True, exist_ok=True)
    svc_path.write_text(content)
    print(f"Service file written to {svc_path}")

    # Enable and start
    if platform == "linux":
        _install_systemd(svc_path)
    else:
        _install_launchd(svc_path)


def _install_systemd(svc_path: Path) -> None:
    """Enable and start the systemd user service."""
    result = _run(["systemctl", "--user", "daemon-reload"])
    if result.returncode != 0:
        print(f"Warning: daemon-reload failed: {result.stderr}", file=sys.stderr)

    result = _run(["systemctl", "--user", "enable", "open-udang.service"])
    if result.returncode != 0:
        print(f"Warning: enable failed: {result.stderr}", file=sys.stderr)

    result = _run(["systemctl", "--user", "start", "open-udang.service"])
    if result.returncode != 0:
        print(f"Warning: start failed: {result.stderr}", file=sys.stderr)

    # Enable lingering so the service runs without an active login session
    result = _run(["loginctl", "enable-linger"], check=False)
    if result.returncode != 0:
        print(
            "\nNote: Could not enable login lingering. The service may stop when "
            "you log out. Run manually:\n"
            f"  loginctl enable-linger {os.environ.get('USER', '')}"
        )

    print("\nOpenUdang is installed and running as a systemd user service.")
    print("\nUseful commands:")
    print("  systemctl --user status open-udang   # check status")
    print("  journalctl --user -u open-udang -f   # follow logs")
    print("  systemctl --user restart open-udang   # restart")
    print("  openudang uninstall                   # remove the service")


def _install_launchd(svc_path: Path) -> None:
    """Load the launchd user agent."""
    result = _run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(svc_path)],
        check=False,
    )
    if result.returncode != 0:
        print(f"Warning: launchctl bootstrap failed: {result.stderr}", file=sys.stderr)

    print("\nOpenUdang is installed and running as a launchd user agent.")
    print("\nUseful commands:")
    print(f"  launchctl list | grep {_LAUNCHD_LABEL}           # check status")
    print(f"  tail -f ~/Library/Logs/OpenUdang/openudang.stderr.log  # follow logs")
    print(f"  launchctl kickstart gui/{os.getuid()}/{_LAUNCHD_LABEL}  # restart")
    print("  openudang uninstall                                     # remove the service")


def uninstall_service() -> None:
    """Remove the OpenUdang system service.

    On Linux, stops, disables, and removes the systemd user unit.
    On macOS, unloads and removes the launchd user agent.
    """
    platform = _detect_platform()
    svc_path = _service_path(platform)

    if not svc_path.exists():
        print("OpenUdang service is not installed.")
        return

    if platform == "linux":
        _uninstall_systemd(svc_path)
    else:
        _uninstall_launchd(svc_path)


def _uninstall_systemd(svc_path: Path) -> None:
    """Stop, disable, and remove the systemd user service."""
    _run(["systemctl", "--user", "stop", "open-udang.service"], check=False)
    _run(["systemctl", "--user", "disable", "open-udang.service"], check=False)
    svc_path.unlink()
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    print("OpenUdang systemd service has been removed.")


def _uninstall_launchd(svc_path: Path) -> None:
    """Unload and remove the launchd user agent."""
    _run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(svc_path)],
        check=False,
    )
    svc_path.unlink()
    print("OpenUdang launchd agent has been removed.")
    print(f"Log files remain at {_LAUNCHD_LOG_DIR} — delete manually if desired.")
