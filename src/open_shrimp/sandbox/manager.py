"""Sandbox manager: global lifecycle, factory, and build logging.

The :class:`SandboxManager` protocol abstracts global sandbox concerns
(reaper lifecycle, instance naming, container cleanup, build logging)
away from the per-instance :class:`~open_shrimp.sandbox.base.Sandbox`
protocol.  Callers interact with a single manager instance threaded
through ``bot_data``; individual sandboxes are obtained via
:meth:`SandboxManager.create_sandbox`.

Use :func:`create_sandbox_managers` to instantiate the correct backends
for the current platform and configuration.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from open_shrimp.config import Config, ContextConfig
from open_shrimp.paths import build_log_dir as _build_log_dir, data_dir as _data_dir
from open_shrimp.sandbox.base import Sandbox

logger = logging.getLogger(__name__)

# Graceful shutdown timeout before falling back to destroy.
_SHUTDOWN_TIMEOUT = 180


# ---------------------------------------------------------------------------
# Global build registry
# ---------------------------------------------------------------------------
# Authoritative source of truth for active builds.  Each
# ``register_build`` / ``unregister_build`` call updates this registry so
# that ``resolve_container_build`` can look up the owning manager without
# iterating managers and guessing based on shared file paths.

_build_registry: dict[str, tuple[Path, SandboxManager]] = {}
_build_registry_lock = threading.Lock()


def register_active_build(
    context_name: str, log_path: Path, manager: SandboxManager,
) -> None:
    """Record an active build in the global registry."""
    with _build_registry_lock:
        _build_registry[context_name] = (log_path, manager)


def unregister_active_build(context_name: str) -> None:
    """Remove a build from the global registry."""
    with _build_registry_lock:
        _build_registry.pop(context_name, None)


def lookup_active_build(
    context_name: str,
) -> tuple[Path, SandboxManager] | None:
    """Look up an active build by context name.

    Returns ``(log_path, manager)`` if the build is registered, else
    ``None``.
    """
    with _build_registry_lock:
        return _build_registry.get(context_name)


def destroy_contexts_background(
    context_names: set[str],
    managers: dict[str, SandboxManager],
) -> None:
    """Run ``destroy_context`` for each context on all managers in a daemon thread."""

    def _run() -> None:
        for ctx_name in context_names:
            logger.info("Cleaning up sandbox resources for removed context '%s'", ctx_name)
            for mgr in managers.values():
                try:
                    mgr.destroy_context(ctx_name)
                except Exception:
                    logger.warning(
                        "Error cleaning up context '%s'", ctx_name,
                        exc_info=True,
                    )

    threading.Thread(target=_run, daemon=True).start()


@runtime_checkable
class SandboxManager(Protocol):
    """Manages global sandbox lifecycle and acts as a factory for sandboxes."""

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        """Configure instance-specific naming for multi-instance deployments."""
        ...

    @property
    def instance_prefix(self) -> str:
        """The current instance prefix (e.g. ``"openshrimp"`` or
        ``"openshrimp-mybot"``)."""
        ...

    @property
    def container_label(self) -> str:
        """Docker label used to tag managed containers."""
        ...

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Start crash-safety reaper (Ryuk for Docker, no-op for others)."""
        ...

    def stop_reaper(self) -> None:
        """Stop the crash-safety reaper."""
        ...

    def stop_all(self) -> None:
        """Stop and remove all managed sandbox runtimes."""
        ...

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        """Evict the cached sandbox for *context_name*.

        Stops the runtime (container/VM) and removes it from the cache so
        the next ``create_sandbox`` call builds a fresh instance with
        updated configuration (e.g. new additional directories).
        """
        ...

    def get_active_sandbox(self, context_name: str) -> Sandbox | None:
        """Return the cached sandbox for *context_name*, or ``None``.

        Does not create a new sandbox.  Used by callers that want to
        interact with a sandbox only if it is already running (e.g.
        cleaning up runtime port forwards on ``/clear``).
        """
        ...

    def destroy_context(self, context_name: str) -> None:
        """Permanently destroy all resources for a context.

        Stops the runtime (if running), removes persistent state
        (disk images, cloud-init ISOs, state directories), and
        cleans up any backend-specific resources (Docker images,
        libvirt domain definitions, Lima instances).

        Unlike ``invalidate_sandbox`` which only evicts from cache
        and stops the runtime, this method deletes everything.
        Idempotent — safe to call multiple times.
        """
        ...

    def cleanup_orphans(self, active_contexts: set[str]) -> None:
        """Remove resources for contexts not in *active_contexts*.

        Scans the state directory for context subdirectories and
        destroys any that are not in the active set.  Logs each
        orphan found and cleaned.
        """
        ...

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        """Return a cached or new per-context :class:`Sandbox` instance.

        The same instance is returned for the same *context_name* across
        multiple calls.  The sandbox's lifecycle (VM/container) is
        independent of individual sessions.
        """
        ...

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        """Register an active build, return the log file path."""
        ...

    def unregister_build(self, context_name: str) -> None:
        """Mark a build as no longer active."""
        ...

    def is_build_active(self, context_name: str) -> bool:
        """Check whether a build is currently active."""
        ...

    @property
    def build_log_dir(self) -> Path:
        """Directory containing build log files."""
        ...

    @property
    def state_dir(self) -> Path:
        """Base directory for per-context sandbox state."""
        ...

    def opencode_home_dir(self, context_name: str) -> Path:
        """Host-side directory mapped to OpenCode's data dir in the sandbox."""
        ...


# ---------------------------------------------------------------------------
# Docker implementation
# ---------------------------------------------------------------------------


class DockerSandboxManager:
    """Docker-backed :class:`SandboxManager` implementation.

    Lifts the module-level globals from :mod:`open_shrimp.sandbox.docker_helpers` into
    instance attributes so the manager can be injected and tested.
    """

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"
        self._ryuk_socket: socket.socket | None = None
        self._ryuk_container_id: str | None = None
        self._sandbox_cache: dict[str, Sandbox] = {}

        # Build logging state.
        self._active_builds: dict[str, Path] = {}
        self._active_builds_lock = threading.Lock()

        self._build_log_dir = _build_log_dir()
        self._state_dir = _data_dir() / "containers"

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"
        # Keep the legacy module globals in sync so that free functions in
        # container.py (called by DockerSandbox) see the right prefix.
        import open_shrimp.sandbox.docker_helpers as _c
        _c._INSTANCE_PREFIX = self._instance_prefix  # noqa: SLF001
        _c._CONTAINER_LABEL = self._container_label  # noqa: SLF001
        if instance_name:
            _c.CONTAINER_IMAGE = f"openshrimp-{instance_name}-claude:latest"
            _c.COMPUTER_USE_IMAGE = f"openshrimp-{instance_name}-computer-use:latest"
        else:
            _c.CONTAINER_IMAGE = "openshrimp-claude:latest"
            _c.COMPUTER_USE_IMAGE = "openshrimp-computer-use:latest"

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Start Testcontainers Ryuk and register a label filter.

        Ryuk watches a TCP connection as a liveness signal.  When the
        connection drops (bot crash/exit), Ryuk reaps labelled containers.
        """
        from open_shrimp.sandbox.docker_helpers import RYUK_IMAGE, check_docker_available

        if not check_docker_available():
            return

        prefix = self._instance_prefix
        label = self._container_label

        try:
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name", f"{prefix}-ryuk",
                    "-v", "/var/run/docker.sock:/var/run/docker.sock",
                    "-p", "127.0.0.1::8080",
                    "--label", f"{label}.ryuk=true",
                    RYUK_IMAGE,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                if "Conflict" in result.stderr or "already in use" in result.stderr:
                    subprocess.run(
                        ["docker", "rm", "-f", f"{prefix}-ryuk"],
                        capture_output=True,
                    )
                    result = subprocess.run(
                        [
                            "docker", "run", "-d",
                            "--name", f"{prefix}-ryuk",
                            "-v", "/var/run/docker.sock:/var/run/docker.sock",
                            "-p", "127.0.0.1::8080",
                            "--label", f"{label}.ryuk=true",
                            RYUK_IMAGE,
                        ],
                        capture_output=True,
                        text=True,
                    )
                if result.returncode != 0:
                    logger.warning(
                        "Failed to start Ryuk container: %s",
                        result.stderr.strip(),
                    )
                    return

            self._ryuk_container_id = result.stdout.strip()
            logger.info(
                "Started Ryuk container: %s", self._ryuk_container_id[:12],
            )

            # Discover the mapped host port.
            port_result = subprocess.run(
                ["docker", "port", f"{prefix}-ryuk", "8080"],
                capture_output=True,
                text=True,
            )
            if port_result.returncode != 0:
                logger.warning(
                    "Failed to get Ryuk port: %s",
                    port_result.stderr.strip(),
                )
                self._cleanup_ryuk_container()
                return

            port_str = port_result.stdout.strip().rsplit(":", 1)[-1]
            port = int(port_str)

            # Connect and register our label filter.
            import time as _time

            filter_msg = f"label={label}=true\n".encode()
            sock: socket.socket | None = None
            for _attempt in range(10):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(5)
                    sock.connect(("127.0.0.1", port))
                    sock.sendall(filter_msg)
                    ack = sock.recv(1024).decode().strip()
                    if ack == "ACK":
                        break
                    logger.warning("Unexpected Ryuk response: %s", ack)
                    sock.close()
                    sock = None
                except (ConnectionResetError, ConnectionRefusedError, OSError):
                    if sock is not None:
                        sock.close()
                        sock = None
                    _time.sleep(0.2)
            else:
                logger.warning("Could not connect to Ryuk after retries")
                self._cleanup_ryuk_container()
                return

            sock.settimeout(None)
            self._ryuk_socket = sock
            logger.info(
                "Ryuk connected on port %d, label filter registered", port,
            )

        except Exception:
            logger.warning(
                "Failed to start Ryuk (continuing without crash cleanup)",
                exc_info=True,
            )
            self._cleanup_ryuk_container()

    def stop_reaper(self) -> None:
        """Close the Ryuk connection and remove the Ryuk container."""
        if self._ryuk_socket is not None:
            try:
                self._ryuk_socket.close()
            except OSError:
                pass
            self._ryuk_socket = None
        self._cleanup_ryuk_container()

    def _cleanup_ryuk_container(self) -> None:
        if self._ryuk_container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", f"{self._instance_prefix}-ryuk"],
                capture_output=True,
            )
            logger.info("Removed Ryuk container")
            self._ryuk_container_id = None

    def stop_all(self) -> None:
        """Stop and remove all OpenShrimp-managed containers."""
        self._sandbox_cache.clear()
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label={self._container_label}=true",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
        )
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if name:
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True,
                )
                logger.info("Removed container %s", name)

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            try:
                cached.stop()
            except Exception:
                logger.debug("Error stopping sandbox %s", context_name, exc_info=True)
            logger.info("Invalidated Docker sandbox for context '%s'", context_name)

    def get_active_sandbox(self, context_name: str) -> Sandbox | None:
        return self._sandbox_cache.get(context_name)

    def destroy_context(self, context_name: str) -> None:
        self.invalidate_sandbox(context_name)

        import open_shrimp.sandbox.docker_helpers as _dh

        # Force-remove the container (may already be gone from invalidate).
        cname = _dh.container_name(context_name)
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)

        # Remove per-context Docker image (custom Dockerfile tag only,
        # not the shared base images).
        repo = _dh.CONTAINER_IMAGE.rsplit(":", 1)[0]
        image_tag = f"{repo}:{context_name}"
        result = subprocess.run(
            ["docker", "rmi", image_tag], capture_output=True,
        )
        if result.returncode == 0:
            logger.info("Removed Docker image %s", image_tag)

        state_path = self._state_dir / context_name
        shutil.rmtree(state_path, ignore_errors=True)

        self.unregister_build(context_name)
        logger.info("Destroyed Docker resources for context '%s'", context_name)

    def cleanup_orphans(self, active_contexts: set[str]) -> None:
        if not self._state_dir.exists():
            return
        for child in self._state_dir.iterdir():
            if child.is_dir() and child.name not in active_contexts:
                logger.info("Orphan Docker context found: %s", child.name)
                self.destroy_context(child.name)

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        assert context.container is not None
        from open_shrimp.sandbox.docker import DockerSandbox

        sandbox = DockerSandbox(
            context_name=context_name,
            project_dir=context.directory,
            additional_directories=context.additional_directories or None,
            docker_in_docker=context.container.docker_in_docker,
            computer_use=context.container.computer_use,
            custom_dockerfile=context.container.dockerfile,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        with self._active_builds_lock:
            self._active_builds[context_name] = log_path
        register_active_build(context_name, log_path, self)
        logger.info(
            "Registered build log for context '%s': %s",
            context_name, log_path,
        )
        return log_path

    def unregister_build(self, context_name: str) -> None:
        with self._active_builds_lock:
            self._active_builds.pop(context_name, None)
        unregister_active_build(context_name)
        logger.info("Unregistered build for context '%s'", context_name)

        log_path = self._build_log_dir / f"{context_name}.log"

        def _cleanup() -> None:
            try:
                log_path.unlink(missing_ok=True)
                logger.debug("Cleaned up build log %s", log_path)
            except Exception:
                logger.debug("Failed to clean up build log %s", log_path)

        timer = threading.Timer(3600, _cleanup)
        timer.daemon = True
        timer.start()

    def is_build_active(self, context_name: str) -> bool:
        with self._active_builds_lock:
            return context_name in self._active_builds

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def opencode_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name / "opencode-home"


# ---------------------------------------------------------------------------
# Lima implementation
# ---------------------------------------------------------------------------


class LimaSandboxManager:
    """Lima-backed :class:`SandboxManager` for macOS VM isolation.

    Uses Lima (Apple Virtualization.framework via the VZ driver) for full
    VM isolation.  The ``limactl`` binary is auto-downloaded on first use.
    """

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"  # unused, but protocol requires it
        self._limactl_path: str | None = None
        self._sandbox_cache: dict[str, Sandbox] = {}

        self._active_builds: dict[str, Path] = {}
        self._active_builds_lock = threading.Lock()

        self._build_log_dir = _build_log_dir()
        self._state_dir = _data_dir() / "lima"

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Ensure limactl binary is available (auto-download if needed)."""
        from open_shrimp.sandbox.lima_helpers import ensure_limactl_sync

        self._limactl_path = ensure_limactl_sync()

    def stop_reaper(self) -> None:
        pass

    def stop_all(self) -> None:
        """Stop all OpenShrimp-managed Lima instances."""
        if self._limactl_path is None:
            self._sandbox_cache.clear()
            return

        from open_shrimp.sandbox.lima_helpers import (
            limactl_list_json,
            limactl_stop,
            _lima_env,
        )

        prefix = self._instance_prefix + "-"
        for inst in limactl_list_json(self._limactl_path):
            name = inst.get("name", "")
            if not name.startswith(prefix):
                continue
            if inst.get("status") == "Running":
                limactl_stop(self._limactl_path, name)
                logger.info("Stopped Lima instance %s", name)

        self._sandbox_cache.clear()

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            try:
                cached.stop()
            except Exception:
                logger.debug("Error stopping Lima sandbox %s", context_name, exc_info=True)
            logger.info("Invalidated Lima sandbox for context '%s'", context_name)

    def get_active_sandbox(self, context_name: str) -> Sandbox | None:
        return self._sandbox_cache.get(context_name)

    def destroy_context(self, context_name: str) -> None:
        self.invalidate_sandbox(context_name)

        # Delete the Lima instance via limactl (handles stop + delete).
        if self._limactl_path is not None:
            from open_shrimp.sandbox.lima_helpers import (
                instance_name as _instance_name,
                limactl_delete,
            )
            inst_name = _instance_name(context_name, self._instance_prefix)
            try:
                limactl_delete(self._limactl_path, inst_name)
                logger.info("Deleted Lima instance %s", inst_name)
            except Exception:
                logger.debug(
                    "Failed to delete Lima instance %s", inst_name,
                    exc_info=True,
                )

        from open_shrimp.sandbox.lima_helpers import state_dir_for
        shutil.rmtree(state_dir_for(context_name), ignore_errors=True)
        shutil.rmtree(self._state_dir / context_name, ignore_errors=True)

        self.unregister_build(context_name)
        logger.info("Destroyed Lima resources for context '%s'", context_name)

    def cleanup_orphans(self, active_contexts: set[str]) -> None:
        seen: set[str] = set()

        # Manager-level state dir (lima/).
        if self._state_dir.exists():
            for child in self._state_dir.iterdir():
                if child.is_dir() and child.name not in active_contexts:
                    seen.add(child.name)

        # Per-context state dir (lima-state/).
        lima_state_base = _data_dir() / "lima-state"
        if lima_state_base.exists():
            for child in lima_state_base.iterdir():
                if child.is_dir() and child.name not in active_contexts:
                    seen.add(child.name)

        for orphan in seen:
            logger.info("Orphan Lima context found: %s", orphan)
            self.destroy_context(orphan)

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        if self._limactl_path is None:
            raise RuntimeError(
                "Lima not available — either start_reaper() was not called "
                "or limactl could not be downloaded. Install with: "
                "brew install lima"
            )
        assert context.sandbox is not None

        from open_shrimp.sandbox.lima import LimaSandbox

        sandbox = LimaSandbox(
            context_name=context_name,
            config=context.sandbox,
            project_dir=context.directory,
            limactl_path=self._limactl_path,
            additional_directories=context.additional_directories or None,
            instance_prefix=self._instance_prefix,
            computer_use=context.sandbox.computer_use,
            guest_os=context.sandbox.guest_os,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        with self._active_builds_lock:
            self._active_builds[context_name] = log_path
        register_active_build(context_name, log_path, self)
        logger.info(
            "Registered build log for context '%s': %s",
            context_name, log_path,
        )
        return log_path

    def unregister_build(self, context_name: str) -> None:
        with self._active_builds_lock:
            self._active_builds.pop(context_name, None)
        unregister_active_build(context_name)
        logger.info("Unregistered build for context '%s'", context_name)

        log_path = self._build_log_dir / f"{context_name}.log"

        def _cleanup() -> None:
            try:
                log_path.unlink(missing_ok=True)
            except Exception:
                pass

        timer = threading.Timer(3600, _cleanup)
        timer.daemon = True
        timer.start()

    def is_build_active(self, context_name: str) -> bool:
        with self._active_builds_lock:
            return context_name in self._active_builds

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def opencode_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name / "opencode-home"


# ---------------------------------------------------------------------------
# Libvirt implementation
# ---------------------------------------------------------------------------


class LibvirtSandboxManager:
    """Libvirt/QEMU-backed :class:`SandboxManager` implementation.

    Manages VM lifecycle via ``qemu:///session`` (rootless libvirt).
    One persistent ``libvirt.virConnect`` connection for the process lifetime.
    """

    def __init__(self) -> None:
        self._instance_prefix = "openshrimp"
        self._container_label = "openshrimp"  # not used, but protocol requires it
        self._conn: "libvirt.virConnect | None" = None  # type: ignore[name-defined]
        self._sandbox_cache: dict[str, Sandbox] = {}

        self._active_builds: dict[str, Path] = {}
        self._active_builds_lock = threading.Lock()

        self._build_log_dir = _build_log_dir()
        self._state_dir = _data_dir() / "vms"

    # -- Instance naming ------------------------------------------------------

    def set_instance_prefix(self, instance_name: str | None) -> None:
        if instance_name:
            self._instance_prefix = f"openshrimp-{instance_name}"
            self._container_label = f"openshrimp-{instance_name}"
        else:
            self._instance_prefix = "openshrimp"
            self._container_label = "openshrimp"

    @property
    def instance_prefix(self) -> str:
        return self._instance_prefix

    @property
    def container_label(self) -> str:
        return self._container_label

    # -- Global lifecycle -----------------------------------------------------

    def start_reaper(self) -> None:
        """Open a persistent connection to ``qemu:///session``.

        Also ensures a suitable virtiofsd binary is available,
        downloading one from GitHub releases if the system version
        is missing or too old.

        No Ryuk equivalent needed — libvirt session domains don't survive
        user logout, and we track domain names for cleanup.
        """
        # Ensure virtiofsd is available (auto-download if needed).
        from open_shrimp.sandbox.libvirt_helpers import ensure_virtiofsd

        try:
            ensure_virtiofsd()
        except Exception:
            logger.warning(
                "virtiofsd not available — VMs will fall back to 9p "
                "filesystem sharing (slower)",
                exc_info=True,
            )

        try:
            import libvirt
        except ImportError:
            logger.error(
                "libvirt-python not installed — install with: "
                "pip install libvirt-python  (and: sudo apt install "
                "libvirt-daemon qemu-system-x86)"
            )
            raise

        try:
            import libvirtaio
            import asyncio

            # Register libvirt events on the asyncio event loop if one is
            # running.  This is non-blocking; individual API calls are fast
            # local socket RPCs (~sub-10ms) so true async/await isn't needed.
            try:
                loop = asyncio.get_running_loop()
                libvirtaio.virEventRegisterAsyncIOImpl(loop)
            except RuntimeError:
                # No running event loop — skip async registration.
                pass
        except ImportError:
            pass

        self._conn = libvirt.open("qemu:///session")
        if self._conn is None:
            raise RuntimeError("Failed to connect to qemu:///session")
        logger.info("Connected to qemu:///session")

    def stop_reaper(self) -> None:
        """Close the libvirt connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            logger.info("Closed qemu:///session connection")

    def stop_all(self) -> None:
        """Gracefully shutdown all openshrimp-* domains (with destroy fallback)."""
        if self._conn is None:
            return

        import libvirt
        import time

        prefix = self._instance_prefix + "-"
        try:
            domains = self._conn.listAllDomains()
        except libvirt.libvirtError:
            return

        # Send ACPI shutdown to all active domains first.
        pending: list[tuple[object, str]] = []
        for domain in domains:
            name = domain.name()
            if not name.startswith(prefix):
                continue

            if not domain.isActive():
                continue

            try:
                domain.shutdown()
                logger.info("Sent ACPI shutdown to %s", name)
                pending.append((domain, name))
            except libvirt.libvirtError:
                try:
                    domain.destroy()
                except libvirt.libvirtError:
                    pass
                logger.info("Force-destroyed %s", name)

        # Wait for all domains to shut down in parallel.
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
        while pending and time.monotonic() < deadline:
            still_alive: list[tuple[object, str]] = []
            for domain, name in pending:
                try:
                    if domain.isActive():
                        still_alive.append((domain, name))
                    else:
                        logger.info("Domain %s shut down", name)
                except libvirt.libvirtError:
                    logger.info("Domain %s shut down", name)
            pending = still_alive
            if pending:
                time.sleep(0.5)

        # Force-destroy any remaining domains.
        for domain, name in pending:
            try:
                domain.destroy()
                logger.warning("Force-destroyed %s after timeout", name)
            except libvirt.libvirtError:
                pass

        self._sandbox_cache.clear()

        # Kill any orphaned virtiofsd processes whose sockets live under
        # our state directory.
        self._stop_all_virtiofsd()

    def _stop_all_virtiofsd(self) -> None:
        """Kill virtiofsd processes with socket paths under our state dir."""
        self._stop_virtiofsd_for(str(self._state_dir))

    def _stop_virtiofsd_for(self, path_prefix: str) -> None:
        """Kill virtiofsd processes with socket paths under *path_prefix*.

        Walks ``/proc`` directly instead of shelling out to ``pgrep``.
        """
        import os
        import signal

        proc = Path("/proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            # /proc/<pid>/cmdline uses \0 as separator.
            parts = cmdline.decode(errors="replace").split("\0")
            if not parts or not parts[0].endswith("virtiofsd"):
                continue
            if not any(path_prefix in arg for arg in parts):
                continue
            try:
                pid = int(entry.name)
                os.kill(pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to orphaned virtiofsd (pid=%d)", pid)
            except (ProcessLookupError, PermissionError):
                pass

    # -- Invalidation ----------------------------------------------------------

    def invalidate_sandbox(self, context_name: str) -> None:
        cached = self._sandbox_cache.pop(context_name, None)
        if cached is not None:
            try:
                cached.stop()
            except Exception:
                logger.debug("Error stopping libvirt sandbox %s", context_name, exc_info=True)
            logger.info("Invalidated libvirt sandbox for context '%s'", context_name)

    def get_active_sandbox(self, context_name: str) -> Sandbox | None:
        return self._sandbox_cache.get(context_name)

    def destroy_context(self, context_name: str) -> None:
        self.invalidate_sandbox(context_name)

        # Destroy + undefine the libvirt domain.
        if self._conn is not None:
            try:
                import libvirt
            except ImportError:
                pass
            else:
                from open_shrimp.sandbox.libvirt_helpers import domain_name
                dom_name = domain_name(context_name, self._instance_prefix)
                try:
                    dom = self._conn.lookupByName(dom_name)
                    if dom.isActive():
                        dom.destroy()
                    dom.undefine()
                    logger.info("Undefined libvirt domain %s", dom_name)
                except libvirt.libvirtError:
                    logger.debug("Domain %s not found or already gone", dom_name)

        state_path = self._state_dir / context_name
        self._stop_virtiofsd_for(str(state_path))
        shutil.rmtree(state_path, ignore_errors=True)

        self.unregister_build(context_name)
        logger.info("Destroyed libvirt resources for context '%s'", context_name)

    def cleanup_orphans(self, active_contexts: set[str]) -> None:
        if not self._state_dir.exists():
            return
        for child in self._state_dir.iterdir():
            if child.is_dir() and child.name not in active_contexts:
                logger.info("Orphan libvirt context found: %s", child.name)
                self.destroy_context(child.name)

    # -- Factory --------------------------------------------------------------

    def create_sandbox(
        self, context_name: str, context: ContextConfig,
    ) -> Sandbox:
        cached = self._sandbox_cache.get(context_name)
        if cached is not None:
            return cached

        if self._conn is None:
            raise RuntimeError(
                "Libvirt connection not available — either start_reaper() was "
                "not called or libvirt-python is not installed. Install with: "
                "pip install libvirt-python"
            )
        assert context.sandbox is not None

        from open_shrimp.sandbox.libvirt import LibvirtSandbox

        sandbox = LibvirtSandbox(
            context_name=context_name,
            config=context.sandbox,
            project_dir=context.directory,
            conn=self._conn,
            additional_directories=context.additional_directories or None,
            instance_prefix=self._instance_prefix,
            computer_use=context.sandbox.computer_use,
            virgl=context.sandbox.virgl,
        )
        self._sandbox_cache[context_name] = sandbox
        return sandbox

    # -- Build logging --------------------------------------------------------

    def register_build(self, context_name: str) -> Path:
        self._build_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._build_log_dir / f"{context_name}.log"
        log_path.write_bytes(b"")
        with self._active_builds_lock:
            self._active_builds[context_name] = log_path
        register_active_build(context_name, log_path, self)
        logger.info(
            "Registered build log for context '%s': %s",
            context_name, log_path,
        )
        return log_path

    def unregister_build(self, context_name: str) -> None:
        with self._active_builds_lock:
            self._active_builds.pop(context_name, None)
        unregister_active_build(context_name)
        logger.info("Unregistered build for context '%s'", context_name)

        log_path = self._build_log_dir / f"{context_name}.log"

        def _cleanup() -> None:
            try:
                log_path.unlink(missing_ok=True)
            except Exception:
                pass

        timer = threading.Timer(3600, _cleanup)
        timer.daemon = True
        timer.start()

    def is_build_active(self, context_name: str) -> bool:
        with self._active_builds_lock:
            return context_name in self._active_builds

    @property
    def build_log_dir(self) -> Path:
        return self._build_log_dir

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def opencode_home_dir(self, context_name: str) -> Path:
        return self._state_dir / context_name / "opencode-home"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_sandbox_managers(config: Config) -> dict[str, SandboxManager]:
    """Instantiate one :class:`SandboxManager` per backend used in the config.

    Returns one manager per backend (``"docker"``, ``"libvirt"``, ``"lima"``)
    that is actually referenced by at least one context.

    Returns:
        A dict mapping backend name to its :class:`SandboxManager` instance.
    """
    # Collect all backends used by sandboxed contexts.
    backends: set[str] = set()
    for ctx in config.contexts.values():
        if ctx.sandbox is not None and ctx.sandbox.enabled:
            backends.add(ctx.sandbox.backend)
        elif ctx.container is not None and ctx.container.enabled:
            backends.add("docker")

    managers: dict[str, SandboxManager] = {}
    if "docker" in backends or (not backends and sys.platform != "darwin"):
        managers["docker"] = DockerSandboxManager()
    if "libvirt" in backends:
        managers["libvirt"] = LibvirtSandboxManager()
    if "lima" in backends:
        managers["lima"] = LimaSandboxManager()
    return managers
