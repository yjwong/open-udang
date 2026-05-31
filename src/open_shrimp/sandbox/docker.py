"""Docker-based sandbox for isolated OpenCode execution.

Wraps the free functions in :mod:`open_shrimp.sandbox.docker_helpers` into a
:class:`DockerSandbox` class that implements the :class:`Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import select
import stat
import subprocess
import threading
import time
from pathlib import Path

from open_shrimp.sandbox.base import PortForward, SandboxOpenCodeServer, VncQuirk

import open_shrimp.sandbox.docker_helpers as _dh

from open_shrimp.sandbox.docker_helpers import (
    container_name as _container_name_fn,
    ensure_computer_use_image as _ensure_computer_use_image,
    ensure_container_running as _ensure_container_running,
    ensure_image as _ensure_image,
    get_screenshots_dir as _get_screenshots_dir,
    get_opencode_home_dir as _get_opencode_home_dir,
    get_opencode_host_port as _get_opencode_host_port,
    get_text_input_active as _get_text_input_active,
    get_text_input_state_path as _get_text_input_state_path,
    get_vnc_port as _get_vnc_port,
)

logger = logging.getLogger(__name__)


def _host_opencode_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _sync_opencode_auth(provider_id: str | None, opencode_home: Path) -> None:
    if not provider_id:
        return
    host_auth = _host_opencode_auth_path()
    if not host_auth.is_file():
        logger.debug("No host OpenCode auth file found at %s", host_auth)
        return
    try:
        data = json.loads(host_auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Failed to read host OpenCode auth file %s",
            host_auth,
            exc_info=True,
        )
        return
    if not isinstance(data, dict):
        logger.warning(
            "Ignoring host OpenCode auth file with non-object root: %s",
            host_auth,
        )
        return
    provider_auth = data.get(provider_id) or data.get(provider_id.rstrip("/"))
    if provider_auth is None:
        logger.debug(
            "Host OpenCode auth file has no entry for provider %s",
            provider_id,
        )
        return
    opencode_home.mkdir(parents=True, exist_ok=True)
    target = opencode_home / "auth.json"
    content = json.dumps(
        {provider_id.rstrip("/"): provider_auth},
        separators=(",", ":"),
    )
    target.write_text(content, encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _append_log(log_file: Path | None, line: str) -> None:
    if log_file is None:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except OSError:
        logger.debug("Failed to append OpenCode sandbox log", exc_info=True)


def _wait_for_opencode_ready(
    proc: subprocess.Popen[str], *, log_file: Path | None = None,
    timeout: float = 20.0,
) -> None:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                raise RuntimeError("sandboxed opencode serve exited before readiness")
            continue
        line = proc.stdout.readline()
        if line:
            stripped = line.rstrip()
            if stripped:
                logger.info("[sandbox opencode] %s", stripped)
                _append_log(log_file, stripped)
            if "listening on" in stripped:
                return
            continue
        if proc.poll() is not None:
            raise RuntimeError("sandboxed opencode serve exited before readiness")
        time.sleep(0.05)
    raise RuntimeError("sandboxed opencode serve did not become ready in time")


def _drain_opencode_output(
    proc: subprocess.Popen[str], log_file: Path | None,
) -> None:
    stream = proc.stdout
    if stream is None:
        return
    for line in stream:
        stripped = line.rstrip()
        if stripped:
            logger.debug("[sandbox opencode] %s", stripped)
            _append_log(log_file, stripped)


class DockerSandbox:
    """Docker container sandbox implementing the :class:`Sandbox` protocol.

    Each instance wraps a single context's Docker lifecycle.  The underlying
    functions in :mod:`open_shrimp.sandbox.docker_helpers` are called with the stored
    configuration, so callers only need the protocol methods.
    """

    def __init__(
        self,
        context_name: str,
        project_dir: str,
        additional_directories: list[str] | None = None,
        docker_in_docker: bool = False,
        computer_use: bool = False,
        custom_dockerfile: str | None = None,
    ) -> None:
        self._context_name = context_name
        self._project_dir = project_dir
        self._additional_directories = additional_directories
        self._docker_in_docker = docker_in_docker
        self._computer_use = computer_use
        self._custom_dockerfile = custom_dockerfile

        if custom_dockerfile:
            repo = _dh.CONTAINER_IMAGE.rsplit(":", 1)[0]
            self._image_name = f"{repo}:{context_name}"
        elif computer_use:
            self._image_name = _dh.COMPUTER_USE_IMAGE
        else:
            self._image_name = _dh.CONTAINER_IMAGE
        self._opencode_proc: subprocess.Popen[str] | None = None
        self._opencode_endpoint: SandboxOpenCodeServer | None = None
        self._opencode_password: str | None = None
        self._opencode_drain_thread: threading.Thread | None = None

    # -- Sandbox protocol -----------------------------------------------------

    @property
    def context_name(self) -> str:
        return self._context_name

    @property
    def host_address(self) -> str:
        return "host.docker.internal"

    @property
    def container_name(self) -> str | None:
        return _container_name_fn(self._context_name)

    def environment_ready(self) -> bool:
        result = subprocess.run(
            ["docker", "image", "inspect", self._image_name],
            capture_output=True,
        )
        return result.returncode == 0

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        if self._computer_use and self._custom_dockerfile:
            _ensure_computer_use_image(log_file=log_file)
            _ensure_image(
                image_name=self._image_name,
                dockerfile=self._custom_dockerfile,
                base_image=_dh.COMPUTER_USE_IMAGE,
                log_file=log_file,
            )
        elif self._computer_use:
            _ensure_computer_use_image(
                image_name=self._image_name,
                log_file=log_file,
            )
        else:
            _ensure_image(
                image_name=self._image_name,
                dockerfile=self._custom_dockerfile,
                log_file=log_file,
            )

    def running(self) -> bool:
        result = subprocess.run(
            [
                "docker", "inspect", "-f", "{{.State.Running}}",
                _container_name_fn(self._context_name),
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        _ensure_container_running(
            context_name=self._context_name,
            project_dir=self._project_dir,
            additional_directories=self._additional_directories,
            docker_in_docker=self._docker_in_docker,
            computer_use=self._computer_use,
            image_name=self._image_name,
        )

    def provision_workspace(self) -> None:
        # Docker uses bind mounts — workspace is already in place.
        pass

    def opencode_home_dir(self) -> Path:
        return _get_opencode_home_dir(self._context_name)

    def ensure_opencode_server(
        self, *, log_file: Path | None = None, provider_id: str | None = None,
    ) -> SandboxOpenCodeServer:
        if self._opencode_endpoint is not None and self._opencode_proc is not None:
            if self._opencode_proc.poll() is None:
                return self._opencode_endpoint

        host_port = _get_opencode_host_port(self._context_name)
        if host_port is None:
            logger.info(
                "Recreating container %s to add OpenCode port binding",
                self.container_name,
            )
            self.stop()
            self.ensure_running(log_file=log_file)
            host_port = _get_opencode_host_port(self._context_name)
        if host_port is None:
            raise RuntimeError(
                f"Container {self.container_name} has no OpenCode port binding. "
                "Recreate the sandbox container and try again."
            )

        _sync_opencode_auth(provider_id, self.opencode_home_dir())

        password = secrets.token_hex(32)
        token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
        endpoint = SandboxOpenCodeServer(
            base_url=f"http://127.0.0.1:{host_port}",
            auth_header=f"Basic {token}",
            cleanup_paths=[],
        )
        cmd = [
            "docker", "exec",
            "-e", "HOME=/home/claude",
            "-e", f"OPENCODE_SERVER_PASSWORD={password}",
            "-w", self._project_dir,
            self.container_name,
            "opencode", "serve",
            "--hostname", "0.0.0.0",
            "--port", str(_dh.OPENCODE_GUEST_PORT),
            "--print-logs",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_opencode_ready(proc, log_file=log_file)
        except Exception:
            proc.terminate()
            raise
        self._opencode_proc = proc
        self._opencode_endpoint = endpoint
        self._opencode_password = password
        self._opencode_drain_thread = threading.Thread(
            target=_drain_opencode_output,
            args=(proc, log_file),
            daemon=True,
        )
        self._opencode_drain_thread.start()
        logger.info(
            "Sandbox context '%s': OpenCode server up at %s",
            self._context_name,
            endpoint.base_url,
        )
        return endpoint

    def stop(self) -> None:
        if self._opencode_proc is not None and self._opencode_proc.poll() is None:
            self._opencode_proc.terminate()
            try:
                self._opencode_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._opencode_proc.kill()
        self._opencode_proc = None
        self._opencode_endpoint = None
        name = self.container_name
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            logger.info("Stopped container %s", name)

    def get_screenshots_dir(self) -> Path | None:
        if self._computer_use:
            return _get_screenshots_dir(self._context_name)
        return None

    def get_vnc_port(self) -> int | None:
        if self._computer_use:
            return _get_vnc_port(self._context_name)
        return None

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Docker computer-use runs wayvnc with no authentication.
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        return frozenset()

    def get_text_input_state_path(self) -> Path | None:
        if self._computer_use:
            return _get_text_input_state_path(self._context_name)
        return None

    def get_text_input_active(self) -> bool:
        if self._computer_use:
            return _get_text_input_active(self._context_name)
        return False

    # -- Computer-use operations ------------------------------------------------

    def _exec_in_container_sync(
        self, cmd: list[str], timeout_secs: float = 10.0,
    ) -> tuple[int, str, str]:
        """Run a command inside the container (synchronous)."""
        uid = os.getuid()
        docker_cmd = [
            "docker", "exec",
            "-e", f"XDG_RUNTIME_DIR=/tmp/runtime-{uid}",
            "-e", "WAYLAND_DISPLAY=wayland-0",
            self.container_name,
            *cmd,
        ]
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
        return result.returncode, result.stdout, result.stderr

    def take_screenshot(self, output_path: Path) -> None:
        ts = int(output_path.stem.split("-")[-1]) if "-" in output_path.stem else 0
        container_path = f"/tmp/screenshots/screenshot-{ts}.png"
        rc, _, stderr = self._exec_in_container_sync(["grim", container_path])
        if rc != 0:
            raise RuntimeError(f"grim failed: {stderr.strip()}")

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        rc, _, stderr = self._exec_in_container_sync([
            "sh", "-c",
            f"wlrctl pointer move {x} {y} && wlrctl pointer click {button}",
        ])
        if rc != 0:
            raise RuntimeError(f"click failed: {stderr.strip()}")

    def send_type(self, text: str) -> None:
        rc, _, stderr = self._exec_in_container_sync([
            "wlrctl", "keyboard", "type", text,
        ])
        if rc != 0:
            raise RuntimeError(f"type failed: {stderr.strip()}")

    def send_key(self, key_str: str) -> None:
        _named_key_chars: dict[str, str] = {
            "return": "\n", "enter": "\n",
            "tab": "\t", "escape": "\x1b",
            "backspace": "\x08", "space": " ",
        }

        parts = key_str.split("+")
        if len(parts) > 1:
            modifiers = ",".join(parts[:-1])
            key_name = parts[-1]
            char = _named_key_chars.get(key_name.lower(), key_name)
            cmd = ["wlrctl", "keyboard", "type", char, "modifiers", modifiers]
        else:
            char = _named_key_chars.get(key_str.lower())
            if char is not None:
                cmd = ["wlrctl", "keyboard", "type", char]
            else:
                cmd = ["wlrctl", "keyboard", "type", key_str]

        rc, _, stderr = self._exec_in_container_sync(cmd)
        if rc != 0:
            raise RuntimeError(f"key press failed: {stderr.strip()}")

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        scroll_map = {
            "up": (0, -amount), "down": (0, amount),
            "left": (-amount, 0), "right": (amount, 0),
        }
        dx, dy = scroll_map.get(direction, (0, amount))
        rc, _, stderr = self._exec_in_container_sync([
            "sh", "-c",
            f"wlrctl pointer move {x} {y} && wlrctl pointer scroll {dx} {dy}",
        ])
        if rc != 0:
            raise RuntimeError(f"scroll failed: {stderr.strip()}")

    def focus_window(self, name: str) -> None:
        rc, _, stderr = self._exec_in_container_sync([
            "wlrctl", "toplevel", "focus", name,
        ])
        if rc != 0:
            raise RuntimeError(f"focus failed: {stderr.strip()}")

    def get_clipboard(self) -> str:
        rc, stdout, stderr = self._exec_in_container_sync(
            ["wl-paste", "--no-newline", "--primary"],
        )
        if rc != 0:
            return ""
        return stdout

    def set_clipboard(self, text: str) -> None:
        uid = os.getuid()
        docker_cmd = [
            "docker", "exec", "-i",
            "-e", f"XDG_RUNTIME_DIR=/tmp/runtime-{uid}",
            "-e", "WAYLAND_DISPLAY=wayland-0",
            self.container_name,
            "wl-copy",
        ]
        result = subprocess.run(
            docker_cmd,
            input=text,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if result.returncode != 0:
            raise RuntimeError(f"wl-copy failed: {result.stderr.strip()}")

    # -- Port forwarding ------------------------------------------------------

    def supports_port_forwarding(self) -> bool:
        return False

    def add_port_forward(
        self,
        guest_port: int,
        requested_host_port: int | None,
        scope_key: str | None,
        description: str | None,
    ) -> PortForward:
        raise NotImplementedError(
            "Runtime port forwarding is not supported for Docker sandboxes."
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return False

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        return []

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        pass

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        if not host_paths:
            return []

        name = self.container_name
        assert name is not None

        upload_dir = "/tmp/openshrimp-uploads"

        # Ensure the destination directory exists inside the container.
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", name,
            "mkdir", "-p", upload_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to create upload dir in container %s: %s",
                name, stderr.decode().strip(),
            )
            return list(host_paths)

        result: list[Path] = []
        for host_path in host_paths:
            container_path = Path(upload_dir) / host_path.name
            proc = await asyncio.create_subprocess_exec(
                "docker", "cp", str(host_path),
                f"{name}:{container_path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "docker cp failed for %s -> %s:%s: %s",
                    host_path, name, container_path,
                    stderr.decode().strip(),
                )
                result.append(host_path)
                continue
            result.append(container_path)
            logger.info(
                "Copied attachment into container: %s -> %s:%s",
                host_path, name, container_path,
            )

        return result
