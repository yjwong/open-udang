"""Lima-based sandbox for isolated OpenCode execution on macOS.

Uses Lima (Apple Virtualization.framework via the VZ driver) for full VM
isolation.  VirtioFS provides fast filesystem sharing between the host
and the guest (Linux or macOS).

VMs are **persistent**: one long-lived VM per context, kept warm between
OpenCode sessions.  Cold boot is ~30 s, so VMs should stay running.

Implements the :class:`~open_shrimp.sandbox.base.Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import logging
import secrets
import shlex
import subprocess
import threading
from pathlib import Path

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.base import (
    VNC_QUIRK_RFB_BGRA_PIXEL_FORMAT,
    VNC_QUIRK_RFB_DROPS_SET_ENCODINGS,
    PortForward,
    SandboxOpenCodeServer,
    VncQuirk,
)
from open_shrimp.sandbox.port_forward import (
    SSH_TUNNEL_OPTS,
    PortForwardRegistry,
    allocate_host_port,
    open_ssh_tunnel,
)
from open_shrimp.sandbox.lima_helpers import (
    _lima_env,
    _log,
    generate_lima_yaml,
    instance_name as _instance_name,
    lima_config_fingerprint,
    limactl_create,
    limactl_delete,
    limactl_instance_status,
    limactl_shell_check,
    limactl_start,
    limactl_stop,
    load_config_fingerprint,
    save_config_fingerprint,
    state_dir_for,
    vnc_host_port,
)
from open_shrimp.sandbox.docker import (
    _drain_opencode_output,
    _sync_opencode_auth,
    _wait_for_opencode_ready,
)
from open_shrimp.sandbox.docker_helpers import OPENCODE_GUEST_PORT
from open_shrimp.vnc.rfb_snapshot import RfbSnapshotError, capture_to_png

logger = logging.getLogger(__name__)

# Named key → character mapping for wlrctl keyboard input (Linux guests).
_NAMED_KEY_CHARS: dict[str, str] = {
    "return": "\n", "enter": "\n",
    "tab": "\t", "escape": "\x1b",
    "backspace": "\x08", "space": " ",
}

# macOS key code mapping for osascript (macOS guests).
_MACOS_KEY_CODES: dict[str, int] = {
    "return": 36, "enter": 76,
    "tab": 48, "escape": 53,
    "backspace": 51, "delete": 117,
    "space": 49,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_MACOS_MODIFIER_MAP: dict[str, str] = {
    "ctrl": "control down",
    "control": "control down",
    "alt": "option down",
    "option": "option down",
    "shift": "shift down",
    "super": "command down",
    "cmd": "command down",
    "command": "command down",
    "meta": "command down",
}


class LimaSandbox:
    """Lima VM sandbox implementing the Sandbox protocol.

    Uses Lima with the VZ driver (Apple Virtualization.framework) for
    macOS VM isolation.  Each instance manages one Lima VM for a single
    context.
    """

    def __init__(
        self,
        context_name: str,
        config: SandboxConfig,
        project_dir: str,
        limactl_path: str,
        additional_directories: list[str] | None = None,
        instance_prefix: str = "openshrimp",
        computer_use: bool = False,
        guest_os: str = "linux",
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._limactl = limactl_path
        self._additional_directories = additional_directories or []
        self._instance_prefix = instance_prefix
        self._computer_use = computer_use
        self._guest_os = guest_os

        self._sdir = state_dir_for(context_name)
        self._inst_name = _instance_name(context_name, instance_prefix)
        self._tmp_dir = self._sdir / "tmp"
        self._opencode_home_dir = self._sdir / "opencode-home"
        self._env = _lima_env()  # cached — LIMA_HOME doesn't change

        # SSH tunnel processes for macOS guest port forwarding.
        self._ssh_tunnels: list[subprocess.Popen] = []

        self._port_forwards = PortForwardRegistry()
        self._opencode_endpoint: SandboxOpenCodeServer | None = None
        self._opencode_proc: subprocess.Popen[str] | None = None
        self._opencode_forward: subprocess.Popen[bytes] | None = None
        self._opencode_password: str | None = None
        self._opencode_drain_thread: threading.Thread | None = None

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def host_address(self) -> str:
        return "192.168.5.2"

    @property
    def container_name(self) -> str | None:
        return None

    def environment_ready(self) -> bool:
        """Check if the Lima instance exists (any status)."""
        return limactl_instance_status(self._limactl, self._inst_name) is not None

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        """Create the Lima instance from a generated YAML template.

        Idempotent — only creates if the instance doesn't exist.
        Detects config drift and rebuilds if necessary.
        """
        sdir = self._sdir
        sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Detect config drift.
        desired_fp = lima_config_fingerprint(
            sdir,
            self._config,
            self._project_dir,
            self._additional_directories or None,
            self._computer_use,
            context_name=self._context_name,
            guest_os=self._guest_os,
        )
        saved_fp = load_config_fingerprint(sdir)
        if saved_fp is not None and saved_fp != desired_fp:
            _log(
                log_file,
                "Lima config changed — rebuilding VM from scratch...",
            )
            logger.info(
                "Config fingerprint drifted for %s — triggering rebuild",
                self._inst_name,
            )
            # Delete fingerprint before rebuild.
            (sdir / "config.sha256").unlink(missing_ok=True)
            self._rebuild_vm(log_file=log_file)
            return

        # Check if instance already exists.
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status is not None:
            logger.info(
                "Lima instance %s already exists (status: %s)",
                self._inst_name, status,
            )
            save_config_fingerprint(sdir, desired_fp)
            _log(log_file, "Lima VM environment ready.")
            return

        _log(log_file, f"Setting up Lima VM for '{self._context_name}'...")

        # Ensure shared directories exist on host.
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._opencode_home_dir.mkdir(parents=True, exist_ok=True)

        # Generate YAML template.
        yaml_path = generate_lima_yaml(
            sdir,
            self._config,
            self._project_dir,
            self._additional_directories or None,
            self._computer_use,
            context_name=self._context_name,
            guest_os=self._guest_os,
        )

        # Create the instance (this downloads the image + boots for cloud-init).
        limactl_create(
            self._limactl, self._inst_name, yaml_path, log_file=log_file,
        )

        save_config_fingerprint(sdir, desired_fp)
        _log(log_file, "Lima VM environment ready.")

    def running(self) -> bool:
        """Check if the Lima instance is running and responsive."""
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status != "Running":
            return False
        return limactl_shell_check(self._limactl, self._inst_name)

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        """Start the Lima instance if not running, wait for shell access."""
        status = limactl_instance_status(self._limactl, self._inst_name)
        if status is None:
            raise RuntimeError(
                f"Lima instance {self._inst_name} not found — "
                f"call ensure_environment() first"
            )

        if status != "Running":
            if self._guest_os == "macos":
                # macOS guests often start in DEGRADED state because
                # SSH agent forwarding requires sudo which isn't
                # available until our askpass provision runs.
                # limactl start exits non-zero for DEGRADED, but the
                # VM is still usable — don't treat it as fatal.
                try:
                    limactl_start(
                        self._limactl, self._inst_name, log_file=log_file,
                    )
                except subprocess.CalledProcessError:
                    # Check if the VM came up despite the error.
                    recheck = limactl_instance_status(
                        self._limactl, self._inst_name,
                    )
                    if recheck != "Running":
                        raise
                    logger.warning(
                        "limactl start returned non-zero for %s but VM is "
                        "running (likely DEGRADED state — expected for "
                        "macOS guests before askpass is provisioned)",
                        self._inst_name,
                    )
            else:
                limactl_start(
                    self._limactl, self._inst_name, log_file=log_file,
                )

        # Wait for shell to be responsive.
        if not limactl_shell_check(self._limactl, self._inst_name):
            _log(log_file, "Waiting for VM to be ready...")
            logger.info("Waiting for shell on %s...", self._inst_name)
            import time

            for _ in range(120):
                if limactl_shell_check(self._limactl, self._inst_name):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"Lima instance {self._inst_name} shell not responsive "
                    f"after 120s — instance left running for debugging"
                )

        _log(log_file, "Lima VM ready.")
        logger.info("Lima instance %s is ready", self._inst_name)

        if self._guest_os == "macos":
            from open_shrimp.sandbox.lima_macos_helpers import (
                ensure_mounts_macos,
                reboot_if_first_provision,
            )
            mount_points = [
                self._project_dir,
                *self._additional_directories,
                self._guest_opencode_data_dir(),
            ]

            # Auto-login only takes effect on boot —
            # reboot once after first provisioning.  Do this before
            # mount fixups so we don't have to redo them after reboot.
            reboot_if_first_provision(
                self._limactl, self._inst_name, log_file=log_file,
            )

            # Fix up VirtioFS mount symlinks — the guest agent may have
            # failed on first boot because parent directories didn't exist.
            ensure_mounts_macos(
                self._limactl, self._inst_name, mount_points,
            )

            # Set up SSH tunnels for port forwarding.
            if self._computer_use:
                self._ensure_ssh_tunnels()

    def provision_workspace(self) -> None:
        """Workspace is available through Lima mounts."""
        pass

    def opencode_home_dir(self) -> Path:
        return self._opencode_home_dir

    def ensure_opencode_server(
        self, *, log_file: Path | None = None, provider_id: str | None = None,
    ) -> SandboxOpenCodeServer:
        if self._opencode_endpoint is not None and self._opencode_healthy():
            return self._opencode_endpoint
        self._stop_opencode_server()

        if limactl_instance_status(self._limactl, self._inst_name) != "Running":
            raise RuntimeError("Cannot start OpenCode: Lima VM is not running")

        ssh_config = Path(self._env["LIMA_HOME"]) / self._inst_name / "ssh.config"
        if not ssh_config.is_file():
            raise RuntimeError(
                f"Cannot start OpenCode: Lima ssh.config not found at {ssh_config}"
            )

        _sync_opencode_auth(provider_id, self.opencode_home_dir())

        host_port = allocate_host_port(None, OPENCODE_GUEST_PORT)
        password = secrets.token_hex(32)
        token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
        endpoint = SandboxOpenCodeServer(
            base_url=f"http://127.0.0.1:{host_port}",
            auth_header=f"Basic {token}",
            cleanup_paths=[],
        )

        forward_cmd = [
            "ssh", "-F", str(ssh_config),
            *SSH_TUNNEL_OPTS,
            "-L", f"127.0.0.1:{host_port}:127.0.0.1:{OPENCODE_GUEST_PORT}",
            f"lima-{self._inst_name}",
        ]
        forward = subprocess.Popen(
            forward_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=self._env,
        )
        try:
            forward.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        else:
            err = (forward.stderr.read() if forward.stderr else b"").decode(
                errors="replace"
            ).strip()
            raise RuntimeError(
                "OpenCode SSH tunnel exited immediately "
                f"(rc={forward.returncode}): {err or 'no stderr'}"
            )

        data_dir = self._guest_opencode_data_dir()
        env_prefix = self._opencode_guest_env_prefix(password)
        remote_cmd = (
            f"mkdir -p {shlex.quote(str(Path(data_dir).parent))} && "
            f"cd {shlex.quote(self._project_dir)} && "
            f"{env_prefix} opencode serve --hostname 127.0.0.1 "
            f"--port {OPENCODE_GUEST_PORT} --print-logs"
        )
        proc = subprocess.Popen(
            [
                self._limactl, "shell", self._inst_name,
                "--", "bash", "-lc", remote_cmd,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._env,
        )
        try:
            _wait_for_opencode_ready(proc, log_file=log_file)
        except Exception:
            self._terminate_process(proc)
            self._terminate_process(forward)
            raise

        self._opencode_proc = proc
        self._opencode_forward = forward
        self._opencode_endpoint = endpoint
        self._opencode_password = password
        self._opencode_drain_thread = threading.Thread(
            target=_drain_opencode_output,
            args=(proc, log_file),
            daemon=True,
        )
        self._opencode_drain_thread.start()
        logger.info(
            "Lima context '%s': OpenCode server up at %s",
            self._context_name,
            endpoint.base_url,
        )
        return endpoint

    def stop(self) -> None:
        """Stop the Lima instance and any SSH tunnels."""
        # Reap forward subprocesses before the VM goes away.
        self._stop_opencode_server()
        self._port_forwards.cleanup()
        for proc in self._ssh_tunnels:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._ssh_tunnels.clear()

        status = limactl_instance_status(self._limactl, self._inst_name)
        if status == "Running":
            limactl_stop(self._limactl, self._inst_name)

    def get_screenshots_dir(self) -> Path | None:
        if self._computer_use:
            return self._sdir / "screenshots"
        return None

    def get_vnc_port(self) -> int | None:
        if not self._computer_use:
            return None
        if self._guest_os == "macos":
            # macOS guests render via VZMacGraphics; the patched limactl
            # publishes an _VZVNCServer port through Lima's hostagent,
            # which writes <LIMA_HOME>/<instance>/vncdisplay.
            return self._read_vz_vnc_port()
        # Linux guests use the YAML port-forward to in-VM wayvnc on 5900.
        return vnc_host_port(self._context_name)

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Linux wayvnc and the macOS-guest _VZVNCServer (configured with
        # NoSecurity) both run unauthenticated on localhost; the WS proxy
        # is the access boundary.
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        # The patched limactl drives Apple's _VZVNCServer SPI, which
        # crashes on SetEncodings (RFB type 2), resets on SetPixelFormat
        # (type 0), and advertises a ServerInit pixel format whose shifts
        # don't match the BGRA bytes it puts on the wire.  The proxy
        # strips the offending client messages and rewrites the server's
        # pixel-format advertisement to match the actual byte order.
        if self._computer_use and self._guest_os == "macos":
            return frozenset({
                VNC_QUIRK_RFB_DROPS_SET_ENCODINGS,
                VNC_QUIRK_RFB_BGRA_PIXEL_FORMAT,
            })
        return frozenset()

    def _read_vz_vnc_port(self) -> int | None:
        """Read the bound _VZVNCServer port from Lima's ``vncdisplay`` file.

        File format is ``<host>:<displaynum>`` with ``displaynum =
        port - 5900``.  Lima writes it once after VM start; until then
        the file is absent and the proxy reports "VNC port not available".
        """
        vnc_file = Path(self._env["LIMA_HOME"]) / self._inst_name / "vncdisplay"
        try:
            content = vnc_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        try:
            _host, num = content.rsplit(":", 1)
            return int(num) + 5900
        except ValueError:
            logger.warning(
                "Cannot parse %s: %r (expected host:displaynum)",
                vnc_file, content,
            )
            return None

    def get_text_input_state_path(self) -> Path | None:
        if self._computer_use:
            return self._sdir / "text-input-state-dir" / "text-input-state"
        return None

    def get_text_input_active(self) -> bool:
        if not self._computer_use:
            return False
        try:
            path = self._sdir / "text-input-state-dir" / "text-input-state"
            return path.read_text(encoding="utf-8").strip() == "1"
        except (FileNotFoundError, OSError):
            return False

    # -- Computer-use operations ------------------------------------------------

    def _exec_in_vm_sync(
        self, cmd: str, *, timeout_secs: float = 10.0,
        stdin_data: str | None = None,
    ) -> tuple[int, str, str]:
        """Run a shell command inside the VM via ``limactl shell``.

        *cmd* is a shell command string (passed to ``bash -c``).
        For Linux guests, the Wayland environment is exported automatically.
        """
        if self._guest_os == "macos":
            shell_cmd = cmd
        else:
            shell_cmd = f"export WAYLAND_DISPLAY=wayland-0; {cmd}"
        result = subprocess.run(
            [
                self._limactl, "shell", self._inst_name,
                "--", "bash", "-c", shell_cmd,
            ],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            env=self._env,
        )
        return result.returncode, result.stdout, result.stderr

    def take_screenshot(self, output_path: Path) -> None:
        if self._guest_os == "macos":
            port = self._read_vz_vnc_port()
            if port is None:
                raise RuntimeError(
                    "VZ host VNC port not yet published — is the VM running "
                    "with video.display=vnc and the patched limactl?"
                )
            try:
                capture_to_png("127.0.0.1", port, output_path)
            except RfbSnapshotError as e:
                raise RuntimeError(f"VZ VNC snapshot failed: {e}") from e
            return
        ts = int(output_path.stem.split("-")[-1]) if "-" in output_path.stem else 0
        guest_path = f"/tmp/screenshots/screenshot-{ts}.png"
        rc, _, stderr = self._exec_in_vm_sync(f"grim {guest_path}")
        if rc != 0:
            raise RuntimeError(f"grim failed: {stderr.strip()}")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        if self._guest_os == "macos":
            self._send_click_macos(x, y, button)
        else:
            rc, _, stderr = self._exec_in_vm_sync(
                f"wlrctl pointer move {x} {y} && wlrctl pointer click {button}"
            )
            if rc != 0:
                raise RuntimeError(f"click failed: {stderr.strip()}")

    def send_type(self, text: str) -> None:
        if self._guest_os == "macos":
            self._send_type_macos(text)
        else:
            rc, _, stderr = self._exec_in_vm_sync(
                f"wlrctl keyboard type {shlex.quote(text)}"
            )
            if rc != 0:
                raise RuntimeError(f"type failed: {stderr.strip()}")

    def send_key(self, key_str: str) -> None:
        if self._guest_os == "macos":
            self._send_key_macos(key_str)
            return
        parts = key_str.split("+")
        if len(parts) > 1:
            modifiers = ",".join(parts[:-1])
            key_name = parts[-1]
            char = _NAMED_KEY_CHARS.get(key_name.lower(), key_name)
            cmd = f"wlrctl keyboard type {shlex.quote(char)} modifiers {modifiers}"
        else:
            char = _NAMED_KEY_CHARS.get(key_str.lower(), key_str)
            cmd = f"wlrctl keyboard type {shlex.quote(char)}"

        rc, _, stderr = self._exec_in_vm_sync(cmd)
        if rc != 0:
            raise RuntimeError(f"key press failed: {stderr.strip()}")

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        if self._guest_os == "macos":
            self._send_scroll_macos(x, y, direction, amount)
            return
        scroll_map = {
            "up": (0, -amount), "down": (0, amount),
            "left": (-amount, 0), "right": (amount, 0),
        }
        dx, dy = scroll_map.get(direction, (0, amount))
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl pointer move {x} {y} && wlrctl pointer scroll {dx} {dy}"
        )
        if rc != 0:
            raise RuntimeError(f"scroll failed: {stderr.strip()}")

    def focus_window(self, name: str) -> None:
        if self._guest_os == "macos":
            self._focus_window_macos(name)
            return
        rc, _, stderr = self._exec_in_vm_sync(
            f"wlrctl toplevel focus {shlex.quote(name)}"
        )
        if rc != 0:
            raise RuntimeError(f"focus failed: {stderr.strip()}")

    def get_clipboard(self) -> str:
        if self._guest_os == "macos":
            rc, stdout, _ = self._exec_in_vm_sync("pbpaste")
            return stdout if rc == 0 else ""
        rc, stdout, _ = self._exec_in_vm_sync("wl-paste --no-newline --primary")
        if rc != 0:
            return ""
        return stdout

    def set_clipboard(self, text: str) -> None:
        if self._guest_os == "macos":
            rc, _, stderr = self._exec_in_vm_sync("pbcopy", stdin_data=text)
            if rc != 0:
                raise RuntimeError(f"pbcopy failed: {stderr.strip()}")
            return
        rc, _, stderr = self._exec_in_vm_sync("wl-copy", stdin_data=text)
        if rc != 0:
            raise RuntimeError(f"wl-copy failed: {stderr.strip()}")

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files into the VM via ``limactl copy``."""
        if not host_paths:
            return []

        upload_dir = "/tmp/openshrimp-uploads"

        # Ensure upload directory exists in VM.
        proc = await asyncio.create_subprocess_exec(
            self._limactl, "shell", self._inst_name, "--",
            "mkdir", "-p", upload_dir,
            env=self._env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to create upload dir in VM %s: %s",
                self._inst_name, stderr.decode().strip(),
            )
            return list(host_paths)

        result: list[Path] = []
        for host_path in host_paths:
            vm_path = Path(upload_dir) / host_path.name
            proc = await asyncio.create_subprocess_exec(
                self._limactl, "copy",
                str(host_path),
                f"{self._inst_name}:{vm_path}",
                env=self._env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "limactl copy failed for %s -> %s:%s: %s",
                    host_path, self._inst_name, vm_path,
                    stderr.decode().strip(),
                )
                result.append(host_path)
                continue
            result.append(vm_path)
            logger.info(
                "Copied attachment into VM: %s -> %s:%s",
                host_path, self._inst_name, vm_path,
            )

        return result

    # -- macOS computer-use helpers -------------------------------------------

    def _send_click_macos(self, x: int, y: int, button: str = "left") -> None:
        """Click at coordinates using Python+Quartz CGEvent."""
        btn_map = {
            "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
            "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
            "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
        }
        down_evt, up_evt, btn_const = btn_map.get(button, btn_map["left"])
        py_script = (
            f"from Quartz.CoreGraphics import *; import time; "
            f"p=CGPointMake({x},{y}); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, {btn_const})); "
            f"time.sleep(0.05); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, {down_evt}, p, {btn_const})); "
            f"time.sleep(0.05); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, {up_evt}, p, {btn_const}))"
        )
        rc, _, stderr = self._exec_in_vm_sync(
            f"python3 -c {shlex.quote(py_script)}", timeout_secs=15.0,
        )
        if rc != 0:
            raise RuntimeError(f"click failed: {stderr.strip()}")

    def _send_type_macos(self, text: str) -> None:
        """Type text using osascript keystroke."""
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{escaped}"'
        rc, _, stderr = self._exec_in_vm_sync(
            f"osascript -e {shlex.quote(script)}"
        )
        if rc != 0:
            raise RuntimeError(f"type failed: {stderr.strip()}")

    def _send_key_macos(self, key_str: str) -> None:
        """Press a key or key combo using osascript key code."""
        parts = key_str.split("+")
        key_name = parts[-1].lower()
        modifiers = parts[:-1] if len(parts) > 1 else []

        # Build modifier clause.
        modifier_clause = ""
        if modifiers:
            mod_strs = []
            for m in modifiers:
                mapped = _MACOS_MODIFIER_MAP.get(m.lower())
                if mapped:
                    mod_strs.append(mapped)
            if mod_strs:
                modifier_clause = " using {" + ", ".join(mod_strs) + "}"

        # Use key code for named keys, keystroke for characters.
        key_code = _MACOS_KEY_CODES.get(key_name)
        if key_code is not None:
            script = (
                f'tell application "System Events" to '
                f'key code {key_code}{modifier_clause}'
            )
        else:
            char = key_name.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                f'tell application "System Events" to '
                f'keystroke "{char}"{modifier_clause}'
            )

        rc, _, stderr = self._exec_in_vm_sync(
            f"osascript -e {shlex.quote(script)}"
        )
        if rc != 0:
            raise RuntimeError(f"key press failed: {stderr.strip()}")

    def _send_scroll_macos(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        """Scroll using Python+Quartz CGEvent."""
        scroll_map = {
            "up": (amount, 0),
            "down": (-amount, 0),
            "left": (0, -amount),
            "right": (0, amount),
        }
        dy, dx = scroll_map.get(direction, (-amount, 0))

        # Move mouse to position first, then scroll.
        py_script = (
            f"from Quartz.CoreGraphics import *; "
            f"p=CGPointMake({x},{y}); "
            f"CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, kCGMouseButtonLeft)); "
            f"e=CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, {dy}, {dx}); "
            f"CGEventPost(kCGHIDEventTap, e)"
        )
        rc, _, stderr = self._exec_in_vm_sync(
            f"python3 -c {shlex.quote(py_script)}", timeout_secs=15.0,
        )
        if rc != 0:
            raise RuntimeError(f"scroll failed: {stderr.strip()}")

    def _focus_window_macos(self, name: str) -> None:
        """Focus a window by application name using osascript."""
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "{escaped}" to activate'
        rc, _, stderr = self._exec_in_vm_sync(
            f"osascript -e {shlex.quote(script)}"
        )
        if rc != 0:
            # Fallback: search by window title via System Events.
            script2 = (
                f'tell application "System Events" to set frontmost of '
                f'(first process whose name contains "{escaped}") to true'
            )
            rc2, _, stderr2 = self._exec_in_vm_sync(
                f"osascript -e {shlex.quote(script2)}"
            )
            if rc2 != 0:
                raise RuntimeError(f"focus failed: {stderr2.strip()}")

    # -- Port forwarding ------------------------------------------------------

    def supports_port_forwarding(self) -> bool:
        return True

    def add_port_forward(
        self,
        guest_port: int,
        requested_host_port: int | None,
        scope_key: str | None,
        description: str | None,
    ) -> PortForward:
        ssh_config = (
            Path(self._env["LIMA_HOME"]) / self._inst_name / "ssh.config"
        )
        if not ssh_config.is_file():
            raise RuntimeError(
                f"Cannot add port forward: Lima ssh.config not found at "
                f"{ssh_config} — is the VM running?"
            )

        host_port = allocate_host_port(requested_host_port, guest_port)
        cmd = [
            "ssh", "-F", str(ssh_config),
            *SSH_TUNNEL_OPTS,
            "-L", f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
            f"lima-{self._inst_name}",
        ]
        return open_ssh_tunnel(
            cmd,
            guest_port=guest_port,
            host_port=host_port,
            scope_key=scope_key,
            description=description,
            registry=self._port_forwards,
            env=self._env,
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return self._port_forwards.remove(forward_id)

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        return self._port_forwards.list(scope_key)

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        self._port_forwards.cleanup(scope_key)

    # -- SSH tunnel management (macOS guests) ---------------------------------

    def _ensure_ssh_tunnels(self) -> None:
        """Set up SSH port-forwarding tunnels for macOS guest ports.

        macOS Lima guests don't support automatic port forwarding, so
        we use an ``ssh -L`` tunnel for the Chromium CDP port (Playwright
        MCP).  The VNC port is exposed directly on the host by the
        patched ``limactl`` via ``_VZVNCServer`` and needs no tunnel.
        """
        # Check if existing tunnels are still alive.
        alive = [p for p in self._ssh_tunnels if p.poll() is None]
        if alive and len(alive) == len(self._ssh_tunnels):
            return
        self._ssh_tunnels = alive

        # Lima writes a ready-to-use ssh client config in the instance
        # directory under ``LIMA_HOME``, not in our OpenShrimp state dir.
        ssh_config = Path(self._env["LIMA_HOME"]) / self._inst_name / "ssh.config"
        if not ssh_config.is_file():
            logger.warning(
                "Cannot set up SSH tunnels for %s: %s not found",
                self._inst_name, ssh_config,
            )
            return
        ssh_target = f"lima-{self._inst_name}"

        tunnels_needed = [(9222, 9222)]

        for host_port, guest_port in tunnels_needed:
            tunnel_cmd = [
                "ssh", "-F", str(ssh_config),
                "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-L", f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
                ssh_target,
            ]
            try:
                proc = subprocess.Popen(
                    tunnel_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    env=self._env,
                )
                self._ssh_tunnels.append(proc)
                logger.info(
                    "SSH tunnel: localhost:%d -> guest:%d (pid %d)",
                    host_port, guest_port, proc.pid,
                )
            except Exception:
                logger.warning(
                    "Failed to start SSH tunnel for port %d", guest_port,
                    exc_info=True,
                )

    def _guest_home(self) -> str:
        if self._guest_os == "macos":
            return f"/Users/{getpass.getuser()}.guest"
        return f"/home/{getpass.getuser()}.guest"

    def _guest_opencode_data_dir(self) -> str:
        if self._guest_os == "macos":
            return f"{self._guest_home()}/Library/Application Support/opencode"
        return f"{self._guest_home()}/.local/share/opencode"

    def _opencode_guest_env_prefix(self, password: str) -> str:
        home = self._guest_home()
        if self._guest_os == "macos":
            data_parent = f"{home}/Library/Application Support"
        else:
            data_parent = f"{home}/.local/share"
        return (
            f"HOME={shlex.quote(home)} "
            f"XDG_DATA_HOME={shlex.quote(data_parent)} "
            f"OPENCODE_SERVER_PASSWORD={shlex.quote(password)}"
        )

    def _opencode_healthy(self) -> bool:
        return (
            self._opencode_proc is not None
            and self._opencode_proc.poll() is None
            and self._opencode_forward is not None
            and self._opencode_forward.poll() is None
        )

    def _stop_opencode_server(self) -> None:
        if self._opencode_proc is not None:
            self._terminate_process(self._opencode_proc)
        if self._opencode_forward is not None:
            self._terminate_process(self._opencode_forward)
        self._opencode_proc = None
        self._opencode_forward = None
        self._opencode_endpoint = None
        self._opencode_password = None

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # -- Internal helpers -----------------------------------------------------

    def _rebuild_vm(self, *, log_file: Path | None = None) -> None:
        """Delete the Lima instance and recreate from scratch."""
        _log(log_file, "Deleting existing Lima instance for rebuild...")
        limactl_delete(self._limactl, self._inst_name)

        # Re-run ensure_environment to recreate.
        self.ensure_environment(log_file=log_file)
