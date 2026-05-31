"""Lima VM helper functions.

Handles Lima binary management (auto-download), YAML template generation,
limactl CLI wrappers, config fingerprinting, and CLI wrapper script
generation.  All limactl invocations use ``LIMA_HOME`` to isolate
OpenShrimp's VMs from the user's personal Lima instances.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path

import yaml
from open_shrimp.config import SandboxConfig
from open_shrimp.paths import data_dir as _data_dir, get_instance_name as _get_instance_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIMA_VERSION = "2.1.1"


def _bin_dir() -> Path:
    return _data_dir() / "bin"


def _lima_state_dir() -> Path:
    """Return the ``LIMA_HOME`` directory, scoped by instance name when set.

    Lima creates Unix sockets under LIMA_HOME/<instance>/ssh.sock.* which
    must stay below UNIX_PATH_MAX (104 on macOS).  The platformdirs data
    path (~/Library/Application Support/...) is too long, so we use a short
    path under $HOME instead.
    """
    name = _get_instance_name()
    if name:
        return Path.home() / ".openshrimp" / f"lima-{name}"
    return Path.home() / ".openshrimp" / "lima"

def _download_base() -> str:
    """Return the GitHub release base URL to download the lima tarball from.

    OpenShrimp ships a custom-built ``limactl`` patched to attach Apple's
    private ``_VZVNCServer`` SPI to the running ``VZVirtualMachine``,
    enabling a host-side VNC server that doesn't require the ``limactl``
    GUI window to be open. The patched binary is attached to the
    OpenShrimp GitHub release this code shipped in, so a given install
    always pulls the ``limactl`` built against the ``LIMA_VERSION`` it
    pins. ``release.yaml`` lints the ``LIMA_VERSION`` ↔ ``patches/PIN``
    agreement to keep the runtime constant and the build pin coupled.

    Falls back to upstream Lima if the install version can't be
    determined — works in dev, but the patched ``_VZVNCServer`` path
    will be inert there.
    """
    from open_shrimp.updater import _REPO, get_current_version

    version = get_current_version()
    if version != "0.0.0":
        return f"https://github.com/{_REPO}/releases/download/v{version}"
    return f"https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}"

_DOWNLOAD_MAP: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): f"lima-{LIMA_VERSION}-Darwin-arm64.tar.gz",
    ("Darwin", "x86_64"): f"lima-{LIMA_VERSION}-Darwin-x86_64.tar.gz",
}

# Ubuntu 24.04 LTS cloud images.
_CLOUD_IMAGES: dict[str, str] = {
    "aarch64": (
        "https://cloud-images.ubuntu.com/releases/24.04/release/"
        "ubuntu-24.04-server-cloudimg-arm64.img"
    ),
    "x86_64": (
        "https://cloud-images.ubuntu.com/releases/24.04/release/"
        "ubuntu-24.04-server-cloudimg-amd64.img"
    ),
}

# ---------------------------------------------------------------------------
# Lima binary management (following tunnel.py pattern)
# ---------------------------------------------------------------------------


def _find_limactl() -> str | None:
    """Find limactl: check managed bin dir first, then ``$PATH``."""
    local_bin = _bin_dir() / "limactl"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    path = shutil.which("limactl")
    if path:
        return path

    return None


def _download_lima_sync() -> str:
    """Download and extract the Lima release tarball (sync).

    Lima tarballs contain a ``bin/`` subdirectory with ``limactl``,
    ``lima``, etc.  All binaries are extracted to ``_bin_dir()``.

    Returns the path to the ``limactl`` binary.
    """
    system = platform.system()
    machine = platform.machine()
    tarball_name = _DOWNLOAD_MAP.get((system, machine))
    if tarball_name is None:
        raise RuntimeError(
            f"Unsupported platform for Lima auto-download: "
            f"{system} {machine}. Please install Lima manually: "
            f"brew install lima"
        )

    bin_dir = _bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    url = f"{_download_base()}/{tarball_name}"
    logger.info("Downloading Lima %s from %s ...", LIMA_VERSION, url)

    import httpx
    import tarfile

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)

        # Lima expects share/lima/ (guest agents, templates) relative to
        # the install prefix.
        prefix_dir = bin_dir.parent
        with tarfile.open(tmp_path, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.lstrip("./")
                if not member.isfile():
                    continue
                if name.startswith("bin/"):
                    dest = bin_dir / os.path.basename(name)
                elif name.startswith(("share/", "libexec/")):
                    dest = prefix_dir / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                else:
                    continue
                f = tar.extractfile(member)
                if f is not None:
                    with open(dest, "wb") as out:
                        out.write(f.read())
                    dest.chmod(
                        dest.stat().st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )
                    logger.debug("Extracted %s to %s", member.name, dest)
    finally:
        os.unlink(tmp_path)

    target = bin_dir / "limactl"
    if not target.is_file():
        raise RuntimeError("limactl not found in downloaded Lima archive")

    logger.info("Lima %s downloaded to %s", LIMA_VERSION, bin_dir)
    return str(target)


def ensure_limactl_sync() -> str:
    """Ensure limactl is available, downloading if necessary (sync).

    Returns the path to the limactl binary.
    """
    path = _find_limactl()
    if path:
        logger.info("Found limactl at %s", path)
        return path

    logger.info("limactl not found, attempting auto-download...")
    return _download_lima_sync()


# ---------------------------------------------------------------------------
# State directory helpers
# ---------------------------------------------------------------------------


def state_dir_for(context_name: str) -> Path:
    """Return per-context state dir (separate from LIMA_HOME).

    This must NOT live under ``_lima_state_dir()`` because Lima treats
    any subdirectory there with a ``lima.yaml`` as an instance.
    """
    return _data_dir() / "lima-state" / context_name


def vnc_host_port(context_name: str) -> int:
    """Return a deterministic VNC host port for a context.

    Lima does not support ``hostPort: 0`` for auto-assignment, so we
    derive a unique port from the context name to avoid collisions when
    multiple computer-use VMs run concurrently.  Uses the range
    49152–65535 (dynamic/private ports per IANA).
    """
    h = int.from_bytes(hashlib.sha256(context_name.encode()).digest())
    return 49152 + (h % (65536 - 49152))


def instance_name(context_name: str, instance_prefix: str = "openshrimp") -> str:
    """Return sanitised Lima instance name.

    Lima instance names must match ``^[a-zA-Z][a-zA-Z0-9_.-]*$``.

    The prefix is intentionally omitted from the name because LIMA_HOME
    already isolates our instances, and the extra length can push Unix
    socket paths past the 104-char UNIX_PATH_MAX limit.
    """
    raw = context_name
    # Replace invalid characters with hyphens.
    sanitised = re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)
    # Ensure it starts with a letter.
    if sanitised and not sanitised[0].isalpha():
        sanitised = "i-" + sanitised
    return sanitised


def _lima_env() -> dict[str, str]:
    """Return environment dict with ``LIMA_HOME`` set for isolation."""
    env = os.environ.copy()
    env["LIMA_HOME"] = str(_lima_state_dir())
    return env


# ---------------------------------------------------------------------------
# Lima YAML template generation
# ---------------------------------------------------------------------------


def generate_lima_yaml(
    sdir: Path,
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None = None,
    computer_use: bool = False,
    *,
    context_name: str = "",
    guest_os: str = "linux",
) -> Path:
    """Generate a Lima YAML template file.

    Writes to ``sdir/lima.yaml`` and returns the path.
    """
    if guest_os == "macos":
        from open_shrimp.sandbox.lima_macos_helpers import generate_lima_yaml_macos
        return generate_lima_yaml_macos(
            sdir, config, project_dir, additional_directories,
            computer_use, context_name=context_name,
        )

    sdir.mkdir(parents=True, exist_ok=True)

    # Detect host architecture for cloud image selection.
    machine = platform.machine()
    if machine == "arm64":
        arch = "aarch64"
    else:
        arch = "x86_64"

    images = []
    for img_arch, img_url in _CLOUD_IMAGES.items():
        images.append({"location": img_url, "arch": img_arch})

    # Build mounts.
    mounts = _build_mounts(sdir, project_dir, additional_directories, computer_use)

    # Build provision scripts.
    provision = _build_provision_scripts(config, computer_use)

    # Port forwarding.
    port_forward: list[dict] = []
    if computer_use:
        # VNC server (wayvnc on guest port 5900).
        port_forward.append({
            "guestPort": 5900,
            "hostPort": vnc_host_port(context_name or sdir.name),
            "hostIP": "127.0.0.1",
        })
        # Chromium CDP debugging port for Playwright MCP.
        port_forward.append({
            "guestPort": 9222,
            "hostIP": "127.0.0.1",
        })

    template: dict = {
        "vmType": "vz",
        "vmOpts": {
            "vz": {"rosetta": {"enabled": True, "binfmt": True}},
        },
        "cpus": config.cpus,
        "memory": f"{config.memory}MiB",
        "disk": f"{config.disk_size}GiB",
        "images": images,
        "mountType": "virtiofs",
        "mounts": mounts,
        "provision": provision,
        "containerd": {"system": False, "user": False},
        "ssh": {"forwardAgent": True},
    }

    if port_forward:
        template["portForwards"] = port_forward

    yaml_path = sdir / "lima.yaml"
    yaml_path.write_text(yaml.dump(template, default_flow_style=False, sort_keys=False), encoding="utf-8")
    logger.info("Generated Lima YAML template at %s", yaml_path)
    return yaml_path


def _build_mounts(
    sdir: Path,
    project_dir: str,
    additional_directories: list[str] | None,
    computer_use: bool = False,
) -> list[dict]:
    """Build Lima mount entries."""
    mounts = []

    # Project directory (writable).
    mounts.append({"location": project_dir, "writable": True})

    # Additional directories.
    for d in additional_directories or []:
        mounts.append({"location": d, "writable": True})

    # Lima creates the VM user as <username> with home /home/<username>.guest.
    # getpass.getuser(), not os.getlogin() — the latter returns "root"
    # under launchd (macOS .app), producing a wrong mount point.
    vm_home = f"/home/{getpass.getuser()}.guest"

    # Per-context OpenCode state.  OpenCode's built-in tools run inside
    # the VM server process, so its state must be mounted into the guest
    # rather than shared with the host singleton.
    opencode_home = str(sdir / "opencode-home")
    Path(opencode_home).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": opencode_home,
        "mountPoint": f"{vm_home}/.local/share/opencode",
        "writable": True,
    })

    host_skills = Path.home() / ".claude" / "skills"
    if host_skills.is_dir():
        mounts.append({
            "location": str(host_skills),
            "mountPoint": f"{vm_home}/.claude/skills",
            "writable": False,
        })

    # Host-side tmp directory (for task output files).
    tmp_dir = str(sdir / "tmp")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "location": tmp_dir,
        "mountPoint": "/tmp/claude-1000",
        "writable": True,
    })

    if computer_use:
        # Screenshots directory — grim writes here, host reads for Telegram.
        screenshots_dir = str(sdir / "screenshots")
        Path(screenshots_dir).mkdir(parents=True, exist_ok=True)
        mounts.append({
            "location": screenshots_dir,
            "mountPoint": "/tmp/screenshots",
            "writable": True,
        })

        # Text-input-state directory — seat-keyboard writes focus state here.
        text_input_state_dir = str(sdir / "text-input-state-dir")
        Path(text_input_state_dir).mkdir(parents=True, exist_ok=True)
        mounts.append({
            "location": text_input_state_dir,
            "mountPoint": "/tmp/text-input-state-dir",
            "writable": True,
        })

    return mounts


def _build_provision_scripts(
    config: SandboxConfig,
    computer_use: bool = False,
) -> list[dict]:
    """Build Lima provision script entries."""
    scripts = []

    # Base system setup.
    base_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # Create claude user if not exists.
        id claude &>/dev/null || useradd -m -s /bin/bash -G sudo claude
        echo 'claude ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/claude

        # Enable fstrim for disk space reclamation.
        systemctl enable --now fstrim.timer
    """)
    scripts.append({"mode": "system", "script": base_script})

    # User-provided provision script.
    if config.provision:
        scripts.append({"mode": "system", "script": config.provision})

    if computer_use:
        scripts.extend(_build_computer_use_provisions())

    return scripts


def _build_computer_use_provisions() -> list[dict]:
    """Build Lima provision entries for the computer-use desktop stack.

    Installs a headless Wayland compositor (labwc), input injection
    (wlrctl), screenshot capture (grim), VNC server (wayvnc), Google
    Chrome, and systemd user units to auto-start everything.
    """
    provisions: list[dict] = []

    # --- System provision: install packages ---
    install_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # Wayland compositor + tools.
        apt-get update
        apt-get install -y --no-install-recommends \\
            labwc \\
            grim \\
            wayvnc \\
            wl-clipboard \\
            foot \\
            fonts-liberation \\
            fonts-noto-color-emoji \\
            fonts-noto \\
            dbus-x11 \\
            procps

        # Build wlrctl from source (not packaged for arm64).
        apt-get install -y --no-install-recommends \\
            gcc libc6-dev git pkg-config meson ninja-build \\
            libwayland-dev libxkbcommon-dev wayland-protocols
        git clone https://git.sr.ht/~brocellous/wlrctl /tmp/wlrctl
        meson setup --prefix=/usr/local /tmp/wlrctl/build /tmp/wlrctl
        ninja -C /tmp/wlrctl/build install
        rm -rf /tmp/wlrctl

        # Node.js for Playwright MCP.
        curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
        apt-get install -y --no-install-recommends nodejs

        # Install browser: Google Chrome on amd64, Chromium from apt on arm64.
        if [ "$(dpkg --print-architecture)" = "amd64" ]; then
            wget -q -O /tmp/google-chrome.deb \
                'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb'
            apt-get install -y /tmp/google-chrome.deb
            rm /tmp/google-chrome.deb
        else
            apt-get install -y chromium-browser
        fi

        rm -rf /var/lib/apt/lists/*

        # Install Playwright MCP globally.
        npm install -g --cache /tmp/npm-cache \\
            @playwright/mcp
        rm -rf /tmp/npm-cache

        # Enable linger so user services start on boot without login.
        LIMA_USER=$(getent passwd 1000 | cut -d: -f1)
        loginctl enable-linger "$LIMA_USER"
    """)
    provisions.append({"mode": "system", "script": install_script})

    # --- User provision: download browsers, write configs ---
    user_setup_script = textwrap.dedent("""\
        #!/bin/bash
        set -eux

        # labwc config.
        mkdir -p ~/.config/labwc
        cat > ~/.config/labwc/rc.xml << 'RCXML'
        <?xml version="1.0" encoding="UTF-8"?>
        <labwc_config>
          <core><gap>0</gap></core>
          <theme>
            <name></name>
            <titlebar><height>20</height></titlebar>
            <font name="sans" size="10" />
          </theme>
          <keyboard />
          <mouse />
        </labwc_config>
        RCXML

        # Empty autostart — services handle application startup.
        echo '# Applications started via systemd user units.' > ~/.config/labwc/autostart

        # --- systemd user units ---
        mkdir -p ~/.config/systemd/user

        cat > ~/.config/systemd/user/openshrimp-labwc.service << 'UNIT'
        [Unit]
        Description=labwc Wayland compositor (headless)

        [Service]
        Type=simple
        Environment=WLR_BACKENDS=headless
        Environment=WLR_RENDERER=pixman
        Environment=WLR_HEADLESS_OUTPUTS=1
        Environment=WAYLAND_DISPLAY=wayland-0
        ExecStart=/usr/bin/labwc
        Restart=on-failure
        RestartSec=2

        [Install]
        WantedBy=default.target
        UNIT

        cat > ~/.config/systemd/user/openshrimp-wayvnc.service << 'UNIT'
        [Unit]
        Description=wayvnc VNC server
        After=openshrimp-labwc.service
        Requires=openshrimp-labwc.service

        [Service]
        Type=simple
        Environment=WAYLAND_DISPLAY=wayland-0
        ExecStartPre=/bin/bash -c 'for i in $(seq 1 75); do [ -S "$XDG_RUNTIME_DIR/wayland-0" ] && break; sleep 0.2; done'
        ExecStart=/usr/bin/wayvnc --output=HEADLESS-1 0.0.0.0 5900
        Restart=on-failure
        RestartSec=2

        [Install]
        WantedBy=default.target
        UNIT

        # Browser systemd unit: Google Chrome on amd64, Chromium on arm64.
        if command -v google-chrome >/dev/null 2>&1; then
            BROWSER_BIN=/usr/bin/google-chrome
            BROWSER_NAME="Google Chrome"
        else
            BROWSER_BIN=/usr/bin/chromium-browser
            BROWSER_NAME="Chromium"
        fi
        cat > ~/.config/systemd/user/openshrimp-chromium.service << UNIT
        [Unit]
        Description=${BROWSER_NAME} browser
        After=openshrimp-labwc.service
        Requires=openshrimp-labwc.service

        [Service]
        Type=simple
        Environment=WAYLAND_DISPLAY=wayland-0
        ExecStartPre=/bin/bash -c 'for i in \$(seq 1 75); do [ -S "\$XDG_RUNTIME_DIR/wayland-0" ] && break; sleep 0.2; done'
        ExecStart=${BROWSER_BIN} --no-first-run --no-default-browser-check --disable-background-networking --disable-default-apps --ozone-platform=wayland --user-data-dir=%h/.config/google-chrome-debug --remote-debugging-port=9222 --window-size=1280,720
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
        UNIT

        # Enable all units.
        systemctl --user daemon-reload
        systemctl --user enable openshrimp-labwc.service
        systemctl --user enable openshrimp-wayvnc.service
        systemctl --user enable openshrimp-chromium.service

        # Start services now (VM is booting for the first time).
        systemctl --user start openshrimp-labwc.service
        systemctl --user start openshrimp-wayvnc.service
        systemctl --user start openshrimp-chromium.service
    """)
    provisions.append({"mode": "user", "script": user_setup_script})

    return provisions


# ---------------------------------------------------------------------------
# Config fingerprinting (drift detection)
# ---------------------------------------------------------------------------


def lima_config_fingerprint(
    sdir: Path,
    config: SandboxConfig,
    project_dir: str,
    additional_directories: list[str] | None,
    computer_use: bool,
    *,
    context_name: str = "",
    guest_os: str = "linux",
) -> str:
    """SHA-256 fingerprint of the Lima YAML template content.

    Uses the real state directory so mount paths are stable across
    invocations (matching the libvirt approach).  The YAML is rendered
    in memory — no temporary files are created.
    """
    if guest_os == "macos":
        from open_shrimp.sandbox.lima_macos_helpers import lima_config_fingerprint_macos
        return lima_config_fingerprint_macos(
            sdir, config, project_dir, additional_directories,
            computer_use, context_name=context_name,
        )

    # Build the same template that generate_lima_yaml() would produce,
    # but dump to a string instead of writing a file.  Using the real
    # sdir keeps host-side mount paths deterministic.
    mounts = _build_mounts(sdir, project_dir, additional_directories, computer_use)
    provision = _build_provision_scripts(config, computer_use)

    port_forward: list[dict] = []
    if computer_use:
        port_forward.append({
            "guestPort": 5900,
            "hostPort": vnc_host_port(context_name or sdir.name),
            "hostIP": "127.0.0.1",
        })
        port_forward.append({
            "guestPort": 9222,
            "hostIP": "127.0.0.1",
        })

    template: dict = {
        "vmType": "vz",
        "vmOpts": {
            "vz": {"rosetta": {"enabled": True, "binfmt": True}},
        },
        "cpus": config.cpus,
        "memory": f"{config.memory}MiB",
        "disk": f"{config.disk_size}GiB",
        "images": [
            {"location": url, "arch": arch}
            for arch, url in _CLOUD_IMAGES.items()
        ],
        "mountType": "virtiofs",
        "mounts": mounts,
        "provision": provision,
        "containerd": {"system": False, "user": False},
        "ssh": {"forwardAgent": True},
    }

    if port_forward:
        template["portForwards"] = port_forward

    content = yaml.dump(template, default_flow_style=False, sort_keys=False)
    return hashlib.sha256(content.encode()).hexdigest()


def save_config_fingerprint(sdir: Path, fingerprint: str) -> None:
    """Persist the config fingerprint for drift detection."""
    (sdir / "config.sha256").write_text(fingerprint, encoding="utf-8")


def load_config_fingerprint(sdir: Path) -> str | None:
    """Load the saved config fingerprint, or ``None`` if absent."""
    fp_file = sdir / "config.sha256"
    if fp_file.exists():
        return fp_file.read_text(encoding="utf-8").strip()
    return None


# ---------------------------------------------------------------------------
# limactl CLI wrappers
# ---------------------------------------------------------------------------


def _log(log_file: Path | None, msg: str) -> None:
    """Append a line to the build log file (for terminal mini app)."""
    if log_file is not None:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.flush()


def _run_limactl(
    limactl: str,
    args: list[str],
    *,
    log_file: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a limactl command with ``LIMA_HOME`` set."""
    cmd = [limactl, *args]
    env = _lima_env()

    if log_file is not None and not capture_output:
        # Stream output to log file.
        with open(log_file, "a", encoding="utf-8") as f:
            result = subprocess.run(
                cmd,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                check=check,
                timeout=timeout,
            )
        return result

    return subprocess.run(
        cmd,
        env=env,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        check=check,
        timeout=timeout,
    )


def limactl_create(
    limactl: str,
    name: str,
    template_path: Path,
    *,
    log_file: Path | None = None,
) -> None:
    """Create a Lima instance from a YAML template."""
    _log(log_file, f"Creating Lima instance '{name}'...")
    _run_limactl(
        limactl,
        ["create", f"--name={name}", "--tty=false", str(template_path)],
        log_file=log_file,
        capture_output=False,
        timeout=600,
    )
    logger.info("Created Lima instance %s", name)


def limactl_start(
    limactl: str,
    name: str,
    *,
    log_file: Path | None = None,
) -> None:
    """Start a Lima instance."""
    _log(log_file, f"Starting Lima instance '{name}'...")
    _run_limactl(
        limactl,
        ["start", name],
        log_file=log_file,
        capture_output=False,
        timeout=300,
    )
    logger.info("Started Lima instance %s", name)


def limactl_stop(limactl: str, name: str) -> None:
    """Stop a Lima instance."""
    _run_limactl(limactl, ["stop", name], check=False, timeout=120)
    logger.info("Stopped Lima instance %s", name)


def limactl_delete(limactl: str, name: str) -> None:
    """Delete a Lima instance."""
    _run_limactl(
        limactl, ["delete", "--force", name], check=False, timeout=60,
    )
    logger.info("Deleted Lima instance %s", name)


def limactl_list_json(limactl: str) -> list[dict]:
    """Return parsed JSON from ``limactl list --json``."""
    result = _run_limactl(limactl, ["list", "--json"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    # Lima outputs one JSON object per line (JSONL).
    instances = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                instances.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return instances


def limactl_instance_status(limactl: str, name: str) -> str | None:
    """Return instance status (``Running``, ``Stopped``, etc.) or ``None``."""
    for inst in limactl_list_json(limactl):
        if inst.get("name") == name:
            return inst.get("status")
    return None


def limactl_shell_check(limactl: str, name: str) -> bool:
    """Quick liveness check: ``limactl shell <name> -- true``."""
    result = _run_limactl(
        limactl, ["shell", name, "--", "true"], check=False, timeout=10,
    )
    return result.returncode == 0




# ---------------------------------------------------------------------------
# CLI wrapper script generation
# ---------------------------------------------------------------------------
