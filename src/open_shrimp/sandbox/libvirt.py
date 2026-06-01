"""Libvirt/QEMU-based sandbox for isolated OpenCode execution.

Provides VM-level isolation via KVM/QEMU, managed through libvirt's
``qemu:///session`` (rootless) connection.  The host project directory is
shared with the VM via virtiofs (preferred) or 9p (fallback).

VMs are **persistent**: one long-lived VM per context, kept warm between
OpenCode sessions.  Cold boot is ~13s, so VMs should stay running.

Implements the :class:`~open_shrimp.sandbox.base.Sandbox` protocol.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import shlex
import subprocess
import threading
import time
from pathlib import Path

from open_shrimp.sandbox.base import PortForward, SandboxOpenCodeServer, VncQuirk
from open_shrimp.sandbox.port_forward import (
    SSH_TUNNEL_OPTS,
    PortForwardRegistry,
    allocate_host_port,
    open_ssh_tunnel,
)

from open_shrimp.config import SandboxConfig
from open_shrimp.sandbox.libvirt_helpers import (
    _fs_tag_for_dir,
    create_overlay,
    _persistent_dev_name,
    create_persistent_volume,
    domain_name as _domain_name,
    ensure_base_image,
    ensure_mounts,
    ensure_persistent_mounts,
    ensure_ssh_key,
    extract_fs_tags_from_xml,
    extract_persistent_disks_from_xml,
    extract_vnc_port_from_xml,
    find_free_port,
    find_virtiofsd,
    generate_cloud_init_iso,
    generate_domain_xml,
    cloud_init_fingerprint,
    load_cloud_init_fingerprint,
    load_ssh_port,
    qmp_send_key_combo,
    qmp_screendump,
    qmp_send_mouse_event,
    qmp_send_scroll_event,
    qmp_type_text,
    save_cloud_init_fingerprint,
    save_ssh_port,
    ssh_check_alive,
    start_virtiofsd,
    state_dir_for,
    wait_for_cloud_init,
    wait_for_ssh,
    _log,
)
from open_shrimp.sandbox.docker import (
    _drain_opencode_output,
    _sync_opencode_auth,
    _wait_for_opencode_ready,
)
from open_shrimp.sandbox.docker_helpers import OPENCODE_GUEST_PORT
from open_shrimp.sandbox.skill_paths import existing_global_skill_dirs

logger = logging.getLogger(__name__)

# Graceful shutdown timeout before falling back to destroy.
_SHUTDOWN_TIMEOUT = 180

# UID of the ``claude`` user inside the VM.  Cloud-init creates it as the
# first non-system user, which gets UID 1000 on Ubuntu.
_VM_CLAUDE_UID = 1000


def _tail_file(
    source: Path, dest: Path, stop: threading.Event,
) -> None:
    """Tail *source* and append new content to *dest* until *stop* is set.

    Runs in a background thread during VM boot so serial console output
    streams into the build log for the terminal mini app.  Uses
    ``watchfiles`` (inotify on Linux) to wake immediately on writes,
    like ``tail -f``.
    """
    from watchfiles import watch

    # Wait for the source file to appear (QEMU creates it on domain start).
    while not stop.is_set():
        try:
            src_fh = open(source, "r", encoding="utf-8", errors="replace")
            break
        except FileNotFoundError:
            stop.wait(0.5)
    else:
        return

    try:
        with src_fh, open(dest, "a", encoding="utf-8") as dst_fh:
            # Flush any content already in the file.
            chunk = src_fh.read(8192)
            if chunk:
                dst_fh.write(chunk)
                dst_fh.flush()

            for _changes in watch(
                source, stop_event=stop, rust_timeout=500,
            ):
                while True:
                    chunk = src_fh.read(8192)
                    if not chunk:
                        break
                    dst_fh.write(chunk)
                    dst_fh.flush()
    except Exception:
        pass


class LibvirtSandbox:
    """KVM/QEMU virtual machine sandbox implementing the Sandbox protocol.

    Each instance manages one VM's lifecycle for a single context.
    Uses ``libvirt-python`` for domain management (not ``virsh`` CLI).
    """

    def __init__(
        self,
        context_name: str,
        config: SandboxConfig,
        project_dir: str,
        conn: "libvirt.virConnect",  # type: ignore[name-defined]
        additional_directories: list[str] | None = None,
        instance_prefix: str = "openshrimp",
        computer_use: bool = False,
        virgl: bool = False,
    ) -> None:
        self._context_name = context_name
        self._config = config
        self._project_dir = project_dir
        self._additional_directories = additional_directories or []
        self._conn = conn
        self._instance_prefix = instance_prefix
        self._computer_use = computer_use
        self._virgl = virgl
        self._virtiofsd_procs: list[subprocess.Popen[bytes]] = []
        self._use_virtiofs: bool = find_virtiofsd() is not None

        self._sdir = state_dir_for(context_name)
        self._dom_name = _domain_name(context_name, instance_prefix)
        self._ssh_port: int | None = load_ssh_port(self._sdir)

        # Screenshots directory for computer-use (host-side).
        self._screenshots_dir = self._sdir / "screenshots" if computer_use else None
        if self._screenshots_dir:
            self._screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Host-side directories shared into the VM to mirror Docker's
        # bind-mount approach: task output files and OpenCode state are
        # written to the host so the terminal and resume views can read them.
        self._tmp_dir = self._sdir / "tmp"
        self._opencode_home_dir = self._sdir / "opencode-home"

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
        return "10.0.2.2"

    @property
    def container_name(self) -> str | None:
        return None

    def environment_ready(self) -> bool:
        """Check if the VM environment (overlay, cloud-init, SSH key) exists."""
        sdir = self._sdir
        return (
            (sdir / "overlay.qcow2").exists()
            and (sdir / "cloud-init.iso").exists()
            and (sdir / "ssh_key").exists()
        )

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        """Build the VM environment: base image, overlay, cloud-init, SSH key.

        Idempotent — only does real work on first call.
        """
        import libvirt

        sdir = self._sdir
        sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Detect cloud-init config drift.  Cloud-init only runs on first
        # boot, so if any input that affects the user-data has changed
        # (computer_use, provision script, …) the overlay must be rebuilt.
        desired_fp = cloud_init_fingerprint(self._config, self._computer_use)
        saved_fp = load_cloud_init_fingerprint(sdir)
        if saved_fp is not None and saved_fp != desired_fp:
            _log(
                log_file,
                "Cloud-init config changed — rebuilding VM from scratch...",
            )
            logger.info(
                "Cloud-init fingerprint drifted for %s — triggering rebuild",
                self._dom_name,
            )
            # Delete fingerprint before rebuild to prevent infinite
            # recursion (_rebuild_vm calls ensure_environment again).
            (sdir / "cloud-init.sha256").unlink(missing_ok=True)
            self._rebuild_vm(log_file=log_file)
            return

        _log(log_file, f"Setting up VM environment for '{self._context_name}'...")

        # 1. Base image.
        _log(log_file, "Ensuring base image...")
        base_image = ensure_base_image(
            self._config.base_image, log_file=log_file,
        )

        # 2. SSH key.
        private_key, public_key_path = ensure_ssh_key(sdir)
        public_key = public_key_path.read_text(encoding="utf-8").strip()

        # 3. qcow2 overlay.
        overlay = create_overlay(sdir, base_image, self._config.disk_size)

        # 3a. Persistent volume qcow2 files (survive rebuilds).
        persistent_volumes: list[tuple[str, Path]] = []
        for ppath in self._config.persistent_paths:
            pv_qcow2 = create_persistent_volume(sdir, ppath)
            persistent_volumes.append((ppath, pv_qcow2))

        # 4. Cloud-init ISO (user + SSH only; mounts handled via SSH).
        cloud_init_iso = generate_cloud_init_iso(
            sdir, public_key,
            provision_script=self._config.provision,
            computer_use=self._computer_use,
            persistent_paths=self._config.persistent_paths or None,
        )

        # 5. Allocate SSH port (persistent across restarts).
        if self._ssh_port is None:
            self._ssh_port = find_free_port()
            save_ssh_port(sdir, self._ssh_port)

        # 6. Generate and define the domain XML.
        serial_log = sdir / "serial.log"

        # Ensure host-side shared directories exist.
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._opencode_home_dir.mkdir(parents=True, exist_ok=True)

        # Build shared_dirs list for domain XML: (host_dir, socket | None).
        # The domain must declare virtiofs/9p devices for all dirs even
        # though the guest-side mount is managed via SSH later.
        all_dirs, _, _ = self._shared_dirs_and_overrides()
        shared_dirs_xml: list[tuple[str, Path | None]] = []
        for host_dir in all_dirs:
            if self._use_virtiofs:
                sock = self._virtiofs_socket_for(host_dir)
                shared_dirs_xml.append((host_dir, sock))
            else:
                shared_dirs_xml.append((host_dir, None))

        xml = generate_domain_xml(
            self._dom_name,
            overlay_path=overlay,
            cloud_init_iso=cloud_init_iso,
            serial_log=serial_log,
            ssh_port=self._ssh_port,
            memory_mb=self._config.memory,
            vcpus=self._config.cpus,
            shared_dirs=shared_dirs_xml,
            use_virtiofs=self._use_virtiofs,
            computer_use=self._computer_use,
            virgl=self._virgl,
            persistent_volumes=persistent_volumes,
        )

        # Define domain (idempotent — overwrites if exists).
        # If the domain is active but the desired filesystem devices or
        # persistent disks have changed, we must gracefully stop the VM,
        # re-define, and let ensure_running() restart it.
        desired_tags = {_fs_tag_for_dir(d) for d in all_dirs}
        desired_pvs = {
            _persistent_dev_name(i)
            for i in range(len(persistent_volumes))
        }
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                domain.undefine()
                self._conn.defineXML(xml)
                logger.info("Re-defined domain %s", self._dom_name)
            else:
                # Check if filesystem devices or persistent disks drifted.
                live_xml = domain.XMLDesc(0)
                current_tags = extract_fs_tags_from_xml(live_xml)
                current_pvs = extract_persistent_disks_from_xml(live_xml)
                config_drifted = (
                    current_tags != desired_tags
                    or current_pvs != desired_pvs
                )
                if config_drifted:
                    _log(
                        log_file,
                        "VM config changed — restarting VM "
                        "to apply new config...",
                    )
                    logger.info(
                        "Config drifted for %s: "
                        "fs_tags current=%s desired=%s, "
                        "pvs current=%s desired=%s — stopping for re-define",
                        self._dom_name,
                        current_tags, desired_tags,
                        current_pvs, desired_pvs,
                    )
                    self.stop()
                    # After stop, domain is inactive — undefine and re-define.
                    try:
                        domain = self._conn.lookupByName(self._dom_name)
                        domain.undefine()
                    except libvirt.libvirtError:
                        pass
                    self._conn.defineXML(xml)
                    logger.info(
                        "Re-defined domain %s with updated config",
                        self._dom_name,
                    )
                else:
                    logger.info(
                        "Domain %s already active, config unchanged",
                        self._dom_name,
                    )
        except libvirt.libvirtError as e:
            if e.get_error_code() == 42:  # VIR_ERR_NO_DOMAIN
                self._conn.defineXML(xml)
                logger.info("Defined new domain %s", self._dom_name)
            else:
                raise

        save_cloud_init_fingerprint(sdir, desired_fp)
        _log(log_file, "VM environment ready.")

    def running(self) -> bool:
        """Check if the VM is active and SSH-reachable."""
        import libvirt

        if self._ssh_port is None:
            return False
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                return False
        except libvirt.libvirtError:
            return False
        return ssh_check_alive(self._ssh_port, self._sdir / "ssh_key")

    def ensure_running(
        self, *, log_file: Path | None = None, _rebuild_attempted: bool = False,
    ) -> None:
        """Start the VM if not already running, wait for SSH."""
        import libvirt

        assert self._ssh_port is not None, (
            "SSH port not set — call ensure_environment() first"
        )

        # Ensure virtiofsd daemons are available for domain start.
        #
        # virtiofsd removes its socket once a client (QEMU) connects, so
        # socket existence is only meaningful *before* the VM is running.
        # We use process liveness (_virtiofsd_procs / poll()) when the
        # current OpenShrimp process started them, and domain-active
        # status when we inherited a running VM from a previous process.
        if self._use_virtiofs:
            if self._virtiofsd_procs:
                # We started these — check if they're still alive.
                if any(p.poll() is not None for p in self._virtiofsd_procs):
                    self._reap_dead_virtiofsd()
                    _log(log_file, "Starting virtiofs daemons...")
                    self._start_all_virtiofsd()
            elif not self._is_domain_active():
                # Fresh process and VM is not running — need fresh daemons
                # before domain.create().  If the VM *is* active, old
                # virtiofsd (from a previous OpenShrimp process) is already
                # connected and serving the VM.
                _log(log_file, "Starting virtiofs daemons...")
                self._start_all_virtiofsd()

        # Start domain if not active.
        cold_start = False
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                # Truncate serial.log before boot so the log only shows
                # this boot's output.
                serial_log = self._sdir / "serial.log"
                serial_log.write_bytes(b"")

                _log(log_file, "Starting VM...")
                domain.create()
                cold_start = True
                logger.info("Started domain %s", self._dom_name)
                # Modern virtiofsd (Rust) daemonizes on startup: the
                # Popen child we spawned exits once QEMU has attached to
                # the FUSE socket.  Reap those handles now so they don't
                # linger as zombies for the lifetime of the service — we
                # have observed 15+ piling up between restarts.
                self._reap_dead_virtiofsd()
        except libvirt.libvirtError as e:
            if e.get_error_code() == 42:  # VIR_ERR_NO_DOMAIN
                raise RuntimeError(
                    f"Domain {self._dom_name} not defined — "
                    f"call ensure_environment() first"
                ) from e
            if e.get_error_code() == 55:  # VIR_ERR_OPERATION_INVALID (already running)
                pass
            else:
                raise

        # Wait for SSH connectivity.  While waiting, tail the serial log
        # so the user can see boot progress in the terminal mini app.
        ssh_key = self._sdir / "ssh_key"
        if not ssh_check_alive(self._ssh_port, ssh_key):
            _log(log_file, "Waiting for SSH...")
            logger.info("Waiting for SSH on port %d...", self._ssh_port)

            # Start tailing serial.log to the build log in a background
            # thread so boot output streams to the terminal mini app.
            stop_tail = threading.Event()
            tail_thread: threading.Thread | None = None
            if log_file is not None and cold_start:
                serial_log = self._sdir / "serial.log"
                tail_thread = threading.Thread(
                    target=_tail_file,
                    args=(serial_log, log_file, stop_tail),
                    daemon=True,
                )
                tail_thread.start()

            try:
                if not wait_for_ssh(self._ssh_port, ssh_key, timeout=60):
                    raise RuntimeError(
                        f"VM {self._dom_name} SSH not reachable "
                        f"on port {self._ssh_port} — VM left running "
                        f"for debugging (virsh console, serial.log)"
                    )

                # Wait for cloud-init to finish on cold starts so that all
                # provisioned services (Chrome, compositor, etc.) are
                # running before we declare the VM ready.
                if cold_start:
                    _log(log_file, "Waiting for cloud-init to finish...")
                    logger.info(
                        "Waiting for cloud-init on %s (port %d)...",
                        self._dom_name, self._ssh_port,
                    )
                    if not wait_for_cloud_init(
                        self._ssh_port, self._sdir / "ssh_key",
                    ):
                        if _rebuild_attempted:
                            raise RuntimeError(
                                f"cloud-init failed on {self._dom_name} after "
                                f"rebuild — VM may require manual intervention"
                            )
                        _log(
                            log_file,
                            "cloud-init failed — rebuilding VM from scratch...",
                        )
                        logger.warning(
                            "cloud-init did not complete cleanly on %s "
                            "— triggering rebuild",
                            self._dom_name,
                        )
                        self._rebuild_vm(log_file=log_file)
                        self.ensure_running(
                            log_file=log_file, _rebuild_attempted=True,
                        )
                        return
            finally:
                stop_tail.set()
                if tail_thread is not None:
                    tail_thread.join(timeout=2)

        _log(log_file, "VM ready.")
        logger.info("VM %s SSH ready on port %d", self._dom_name, self._ssh_port)

        # Configure filesystem mounts via SSH (idempotent).
        # This handles config changes (added/removed additional_directories)
        # without requiring a VM rebuild.
        all_dirs, mount_overrides, readonly_dirs = self._shared_dirs_and_overrides()
        fs_type = "virtiofs" if self._use_virtiofs else "9p"
        ensure_mounts(
            ssh_port=self._ssh_port,
            ssh_key=self._sdir / "ssh_key",
            shared_dirs=all_dirs,
            fs_type=fs_type,
            mount_overrides=mount_overrides,
            readonly_dirs=readonly_dirs,
        )

        # Mount persistent volumes (format ext4 if needed, create systemd
        # mount units).  These are block devices, not virtiofs.
        if self._config.persistent_paths:
            ensure_persistent_mounts(
                ssh_port=self._ssh_port,
                ssh_key=self._sdir / "ssh_key",
                persistent_paths=self._config.persistent_paths,
            )

    def provision_workspace(self) -> None:
        """Provision the workspace: ensure OpenCode is installed in the VM."""
        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"

        from open_shrimp.sandbox.docker_helpers import _find_opencode_binary
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        binaries = {"opencode": _find_opencode_binary()}
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        scp_opts = [
            "-i", str(ssh_key),
            "-P", str(self._ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ]

        for name, binary in binaries.items():
            result = subprocess.run(
                ["ssh", *ssh_opts, "claude@localhost", "which", name],
                capture_output=True,
            )
            if result.returncode == 0:
                continue

            logger.info("Installing %s CLI into VM %s...", name, self._dom_name)
            subprocess.run(
                [
                    "scp", *scp_opts,
                    str(binary),
                    f"claude@localhost:/tmp/{name}",
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "ssh", *ssh_opts,
                    "claude@localhost",
                    "--",
                    f"sudo mv /tmp/{name} /usr/local/bin/{name} && "
                    f"sudo chmod +x /usr/local/bin/{name}",
                ],
                check=True,
                capture_output=True,
            )
            logger.info("%s CLI installed in VM %s", name, self._dom_name)

    def opencode_home_dir(self) -> Path:
        return self._opencode_home_dir

    def ensure_opencode_server(
        self, *, log_file: Path | None = None, provider_id: str | None = None,
    ) -> SandboxOpenCodeServer:
        if self._opencode_endpoint is not None and self._opencode_healthy():
            return self._opencode_endpoint
        self._stop_opencode_server()

        if self._ssh_port is None:
            raise RuntimeError("Cannot start OpenCode: libvirt VM is not running")

        _sync_opencode_auth(provider_id, self.opencode_home_dir())

        host_port = allocate_host_port(None, OPENCODE_GUEST_PORT)
        password = secrets.token_hex(32)
        token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
        endpoint = SandboxOpenCodeServer(
            base_url=f"http://127.0.0.1:{host_port}",
            auth_header=f"Basic {token}",
            cleanup_paths=[],
        )

        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        ssh_key = self._sdir / "ssh_key"
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        forward_cmd = [
            "ssh", *ssh_opts, *SSH_TUNNEL_OPTS,
            "-L", f"127.0.0.1:{host_port}:127.0.0.1:{OPENCODE_GUEST_PORT}",
            "claude@localhost",
        ]
        forward = subprocess.Popen(
            forward_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
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

        remote_cmd = (
            f"cd {shlex.quote(self._project_dir)} && "
            "HOME=/home/claude "
            f"OPENCODE_SERVER_PASSWORD={shlex.quote(password)} "
            "opencode serve --hostname 127.0.0.1 "
            f"--port {OPENCODE_GUEST_PORT} --print-logs"
        )
        proc = subprocess.Popen(
            ["ssh", *ssh_opts, "claude@localhost", remote_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
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
            "Libvirt context '%s': OpenCode server up at %s",
            self._context_name,
            endpoint.base_url,
        )
        return endpoint

    def stop(self) -> None:
        """Gracefully shutdown the VM (ACPI), with destroy fallback."""
        import libvirt

        # Reap forward subprocesses before the VM goes away — ssh would
        # die on its own but the Popen handles would linger as zombies.
        self._stop_opencode_server()
        self._port_forwards.cleanup()

        try:
            domain = self._conn.lookupByName(self._dom_name)
        except libvirt.libvirtError:
            return

        if not domain.isActive():
            return

        # Graceful ACPI shutdown.
        try:
            domain.shutdown()
            logger.info("Sent ACPI shutdown to %s", self._dom_name)
        except libvirt.libvirtError:
            logger.warning(
                "ACPI shutdown failed for %s, falling back to destroy",
                self._dom_name,
            )
            domain.destroy()
            # virtiofsd self-terminates when the VM disconnects; reap
            # the child processes so they don't linger as zombies.
            self._reap_dead_virtiofsd()
            return

        # Wait for shutdown to complete.
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if not domain.isActive():
                    logger.info("Domain %s shut down gracefully", self._dom_name)
                    self._reap_dead_virtiofsd()
                    return
            except libvirt.libvirtError:
                self._reap_dead_virtiofsd()
                return
            time.sleep(0.5)

        # Timeout — force destroy.
        logger.warning(
            "Domain %s did not shut down in %ds, destroying",
            self._dom_name, _SHUTDOWN_TIMEOUT,
        )
        try:
            domain.destroy()
        except libvirt.libvirtError:
            pass
        self._reap_dead_virtiofsd()

    def get_screenshots_dir(self) -> Path | None:
        return self._screenshots_dir

    def get_vnc_port(self) -> int | None:
        """Discover the auto-assigned VNC port from the live domain XML."""
        if not self._computer_use:
            return None
        import libvirt
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if not domain.isActive():
                return None
            return extract_vnc_port_from_xml(domain.XMLDesc(0))
        except libvirt.libvirtError:
            return None

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        # Libvirt computer-use runs wayvnc with no authentication.
        return None

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        return frozenset()

    def get_text_input_state_path(self) -> Path | None:
        return None

    def get_text_input_active(self) -> bool:
        return False

    # -- Computer-use operations --------------------------------------------

    def take_screenshot(self, output_path: Path) -> None:
        """Take a screenshot of the VM display via QMP ``screendump``.

        Uses QMP to capture directly from the QEMU display device,
        which works uniformly with and without VirGL and correctly
        captures XWayland windows (unlike ``grim`` / ``wlr-screencopy``).
        """
        qmp_screendump(self._conn, self._dom_name, output_path)

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        """Click at screen coordinates via QMP."""
        qmp_send_mouse_event(
            self._conn, self._dom_name, x, y, button=button,
        )

    def send_type(self, text: str) -> None:
        """Type text via QMP key events."""
        qmp_type_text(self._conn, self._dom_name, text)

    def send_key(self, key_str: str) -> None:
        """Press a key or combo (e.g. ``"ctrl+a"``) via QMP."""
        qmp_send_key_combo(self._conn, self._dom_name, key_str)

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        """Scroll at screen coordinates via QMP."""
        qmp_send_scroll_event(
            self._conn, self._dom_name, x, y, direction, amount,
        )

    def focus_window(self, name: str) -> None:
        """Focus a window by name — not supported in VM contexts."""
        raise NotImplementedError(
            "Window focus via toplevel is not supported in VM contexts. "
            "Use computer_click to click on the desired window, or use "
            "alt+Tab to switch windows."
        )

    def get_clipboard(self) -> str:
        """Get clipboard contents via wl-paste over SSH."""
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        result = subprocess.run(
            [
                "ssh", *ssh_opts, "claude@localhost",
                "env", "XDG_RUNTIME_DIR=/run/user/1000",
                "WAYLAND_DISPLAY=wayland-0",
                "wl-paste", "--no-newline", "--primary",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def set_clipboard(self, text: str) -> None:
        """Set clipboard contents via wl-copy over SSH.

        wl-copy forks a background process to serve paste requests, which
        keeps the SSH connection alive indefinitely. Work around this by
        saving stdin to a tmpfile, then backgrounding wl-copy with nohup
        so the SSH session can exit cleanly.
        """
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"
        ssh_opts = _ssh_common_opts(ssh_key, self._ssh_port)
        # Shell script: save stdin to tmpfile, background wl-copy, exit.
        remote_cmd = (
            'tmpf=$(mktemp);'
            ' cat > "$tmpf";'
            ' env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0'
            ' nohup wl-copy < "$tmpf" >/dev/null 2>&1 &'
            ' sleep 0.1;'
            ' rm "$tmpf"'
        )
        result = subprocess.run(
            ["ssh", *ssh_opts, "claude@localhost", remote_cmd],
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"wl-copy failed: {result.stderr.strip()}")

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files into the VM via scp."""
        if not host_paths:
            return []

        assert self._ssh_port is not None
        ssh_key = self._sdir / "ssh_key"

        upload_dir = "/tmp/openshrimp-uploads"

        # Ensure upload directory exists in VM.
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-i", str(ssh_key),
            "-p", str(self._ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "claude@localhost",
            "mkdir", "-p", upload_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to create upload dir in VM %s: %s",
                self._dom_name, stderr.decode().strip(),
            )
            return list(host_paths)

        result: list[Path] = []
        for host_path in host_paths:
            vm_path = Path(upload_dir) / host_path.name
            proc = await asyncio.create_subprocess_exec(
                "scp",
                "-i", str(ssh_key),
                "-P", str(self._ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                str(host_path),
                f"claude@localhost:{vm_path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "scp failed for %s -> %s:%s: %s",
                    host_path, self._dom_name, vm_path,
                    stderr.decode().strip(),
                )
                result.append(host_path)
                continue
            result.append(vm_path)
            logger.info(
                "Copied attachment into VM: %s -> %s:%s",
                host_path, self._dom_name, vm_path,
            )

        return result

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
        from open_shrimp.sandbox.libvirt_helpers import _ssh_common_opts

        if self._ssh_port is None:
            raise RuntimeError(
                "Cannot add port forward: VM is not running"
            )
        host_port = allocate_host_port(requested_host_port, guest_port)
        cmd = [
            "ssh",
            *_ssh_common_opts(self._sdir / "ssh_key", self._ssh_port),
            *SSH_TUNNEL_OPTS,
            "-L", f"127.0.0.1:{host_port}:127.0.0.1:{guest_port}",
            "claude@localhost",
        ]
        return open_ssh_tunnel(
            cmd,
            guest_port=guest_port,
            host_port=host_port,
            scope_key=scope_key,
            description=description,
            registry=self._port_forwards,
        )

    def remove_port_forward(self, forward_id: str) -> bool:
        return self._port_forwards.remove(forward_id)

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        return self._port_forwards.list(scope_key)

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        self._port_forwards.cleanup(scope_key)

    # -- Internal helpers -----------------------------------------------------

    def _shared_dirs_and_overrides(
        self,
    ) -> tuple[list[str], dict[str, str], set[str]]:
        """Return ``(all_dirs, mount_overrides, readonly_dirs)`` for domain XML / mounts.

        ``all_dirs`` is the list of host directories that need virtiofs/9p
        filesystem devices.  ``mount_overrides`` maps host paths that
        should be mounted at a *different* guest path (tmp and OpenCode state).
        ``readonly_dirs`` is the subset of ``all_dirs`` that should be
        mounted read-only inside the guest.
        """
        all_dirs = [self._project_dir] + self._additional_directories
        if self._screenshots_dir is not None:
            all_dirs.append(str(self._screenshots_dir))
        all_dirs.append(str(self._tmp_dir))
        all_dirs.append(str(self._opencode_home_dir))
        mount_overrides = {
            str(self._tmp_dir): f"/tmp/claude-{_VM_CLAUDE_UID}",
            str(self._opencode_home_dir): "/home/claude/.local/share/opencode",
        }
        readonly_dirs: set[str] = set()
        for host_skills, guest_skills in existing_global_skill_dirs():
            host_skills_str = str(host_skills)
            all_dirs.append(host_skills_str)
            mount_overrides[host_skills_str] = guest_skills
            readonly_dirs.add(host_skills_str)
        return all_dirs, mount_overrides, readonly_dirs

    def _virtiofs_socket_for(self, host_dir: str) -> Path:
        """Return the virtiofsd socket path for a host directory."""
        tag = _fs_tag_for_dir(host_dir)
        return self._sdir / f"{tag}.sock"

    def _start_all_virtiofsd(self) -> None:
        """Start virtiofsd instances for all shared directories."""
        all_dirs, _, readonly_dirs = self._shared_dirs_and_overrides()
        for host_dir in all_dirs:
            sock = self._virtiofs_socket_for(host_dir)
            proc = start_virtiofsd(sock, host_dir, readonly=host_dir in readonly_dirs)
            self._virtiofsd_procs.append(proc)
        # Wait for all sockets to appear.
        import time as _time
        all_socks = [self._virtiofs_socket_for(d) for d in all_dirs]
        for _ in range(20):
            if all(s.exists() for s in all_socks):
                break
            _time.sleep(0.1)

    def _reap_dead_virtiofsd(self) -> None:
        """Reap exited virtiofsd child processes to avoid zombies.

        Does not kill live processes — virtiofsd self-terminates when the
        VM disconnects.  This only collects exit status so the kernel can
        release the process table entries.
        """
        alive: list[subprocess.Popen[bytes]] = []
        for proc in self._virtiofsd_procs:
            if proc.poll() is not None:
                logger.info(
                    "Reaped virtiofsd (pid=%d, rc=%d)", proc.pid, proc.returncode,
                )
            else:
                alive.append(proc)
        self._virtiofsd_procs = alive

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

    def _is_domain_active(self) -> bool:
        """Check if the domain is currently active."""
        import libvirt
        try:
            domain = self._conn.lookupByName(self._dom_name)
            return domain.isActive()
        except libvirt.libvirtError:
            return False

    def _rebuild_vm(self, *, log_file: Path | None = None) -> None:
        """Destroy the VM, delete the overlay, and recreate from scratch.

        Used when SSH is unreachable after boot — typically due to corrupt
        SSH host keys from a hard kill (SIGKILL / virsh destroy).
        """
        import libvirt

        # 1. Destroy + undefine the domain (virtiofsd self-terminates).
        try:
            domain = self._conn.lookupByName(self._dom_name)
            if domain.isActive():
                domain.destroy()
            domain.undefine()
            logger.info("Undefined domain %s for rebuild", self._dom_name)
        except libvirt.libvirtError:
            pass
        # Reap any virtiofsd Popen handles whose processes exited along
        # with the destroyed domain — otherwise they linger as zombies.
        self._reap_dead_virtiofsd()

        # 2. Delete the overlay (forces fresh cloud-init on next boot).
        #    Persistent volume files (pv-*.qcow2) are intentionally preserved
        #    so that data survives rebuilds.
        overlay = self._sdir / "overlay.qcow2"
        overlay.unlink(missing_ok=True)
        # Also delete cloud-init ISO so it gets regenerated.
        (self._sdir / "cloud-init.iso").unlink(missing_ok=True)
        logger.info("Deleted overlay and cloud-init for rebuild")

        # 3. Re-run ensure_environment to regenerate overlay + cloud-init.
        # Do NOT start the domain here — let the caller's ensure_running()
        # handle it so it correctly detects a cold start and waits for
        # cloud-init to complete.
        self.ensure_environment(log_file=log_file)
