"""Sandbox abstraction for isolated agent execution.

Defines the :class:`Sandbox` protocol that encapsulates different isolation
backends (Docker containers, Lima/libvirt VMs, etc.) behind a common lifecycle
interface.  OpenCode paths use :meth:`Sandbox.ensure_opencode_server`.

Use :meth:`SandboxManager.create_sandbox
<open_shrimp.sandbox.manager.SandboxManager.create_sandbox>` to instantiate
the appropriate backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxOpenCodeServer:
    """OpenCode server endpoint owned by a sandbox backend."""

    base_url: str
    auth_header: str
    cleanup_paths: list[Path]


@dataclass(frozen=True)
class PortForward:
    """A live guest→host TCP port forward exposed by a sandbox.

    The host port is always bound to ``127.0.0.1``.  ``scope_key`` is an
    opaque per-conversation key (typically ``"chat_id:thread_id"``) so
    cleanup on ``/clear`` only affects the forwards created from that
    conversation.
    """

    id: str
    guest_port: int
    host_port: int
    scope_key: str | None = None
    description: str | None = None

VNC_QUIRK_RFB_DROPS_SET_ENCODINGS: Final = "rfb_drops_set_encodings"
"""The upstream RFB server crashes on ``SetPixelFormat`` (RFB type 0)
and ``SetEncodings`` (type 2).  The proxy must filter both out of the
client→server byte stream.  Apple's private ``_VZVNCServer`` SPI has
this bug."""

VNC_QUIRK_RFB_BGRA_PIXEL_FORMAT: Final = "rfb_bgra_pixel_format"
"""The upstream RFB server sends 32-bit little-endian BGRA pixels on the
wire but advertises a pixel format in ``ServerInit`` whose shifts don't
match.  Because ``SetPixelFormat`` is dropped (see
:data:`VNC_QUIRK_RFB_DROPS_SET_ENCODINGS`), the only fix is for the proxy
to rewrite the advertised pixel-format on the server→client stream so
clients render the bytes correctly.  Apple's private ``_VZVNCServer``
SPI has this bug — without the rewrite, R and B channels appear swapped
(blue Finder icon shows orange)."""

VncQuirk = Literal["rfb_drops_set_encodings", "rfb_bgra_pixel_format"]
"""A protocol-level workaround the WebSocket VNC proxy must apply."""


@runtime_checkable
class Sandbox(Protocol):
    """Isolated execution environment for an agent runtime.

    A single instance represents one VM/container and is shared across
    multiple sessions (ChatScopes) using the same context.  Instances
    are cached by context name in the :class:`SandboxManager`.

    Lifecycle:
        1. ``ensure_environment()`` — build image / provision VM (slow, idempotent)
        2. ``ensure_running()`` — start container / check SSH (fast when warm)
        3. ``provision_workspace()`` — sync files into sandbox (idempotent)
        4. ``ensure_opencode_server()`` — start/reuse sandboxed OpenCode
        5. ``stop()`` — tear down runtime (VM, container, daemons)
    """

    @property
    def context_name(self) -> str:
        """The context name this sandbox belongs to."""
        ...

    @property
    def container_name(self) -> str | None:
        """Docker container name, or ``None`` for non-container backends."""
        ...

    @property
    def host_address(self) -> str:
        """IP or hostname the sandbox should use to reach the host.

        Each backend has its own network topology — Docker containers
        use ``host.docker.internal``, libvirt VMs use the SLIRP gateway
        ``10.0.2.2``, and Lima VMs use ``192.168.5.2``.
        """
        ...

    def environment_ready(self) -> bool:
        """Return ``True`` if the environment is already built.

        Used by the caller to decide whether to show a "building..." progress
        message before calling :meth:`ensure_environment`.
        """
        ...

    def ensure_environment(self, *, log_file: Path | None = None) -> None:
        """Build image, provision VM, or similar one-time setup.

        Idempotent — safe to call on every invocation.  Only does real work
        when the environment is missing or outdated.  May be slow on first
        call.

        Args:
            log_file: Optional path where build output is streamed
                line-by-line (for the terminal mini app).
        """
        ...

    def running(self) -> bool:
        """Return ``True`` if the runtime is already up.

        Used by the caller to decide whether to show a "starting..." progress
        message before calling :meth:`ensure_running`.  Must be cheap (no
        side effects, no waiting).
        """
        ...

    def ensure_running(self, *, log_file: Path | None = None) -> None:
        """Ensure the runtime is up (container started, SSH reachable, etc.).

        Called before each CLI invocation.  Fast path when already running.

        Args:
            log_file: Optional path where startup output is streamed
                line-by-line (continuation of the build log).
        """
        ...

    def provision_workspace(self) -> None:
        """Provision the workspace filesystem inside the sandbox.

        Called after :meth:`ensure_running`.  For backends where the workspace is
        already available (bind mounts, shared filesystems), this is a
        no-op.  VM backends may use this to clone repositories or sync
        files.

        Idempotent — safe to call on every session start.
        """
        ...

    def ensure_opencode_server(
        self, *, log_file: Path | None = None, provider_id: str | None = None,
    ) -> SandboxOpenCodeServer:
        """Start or reuse ``opencode serve`` inside the sandbox.

        Returns a host-reachable endpoint. Backends that do not implement
        sandboxed OpenCode must raise ``NotImplementedError`` rather than
        falling back to a host OpenCode server.
        """
        ...

    def opencode_home_dir(self) -> Path:
        """Host-side directory mapped to OpenCode's data dir in the sandbox."""
        ...

    def stop(self) -> None:
        """Tear down the runtime (stop container, terminate VM, etc.)."""
        ...

    def get_screenshots_dir(self) -> Path | None:
        """Return host-side screenshots directory, or ``None`` if N/A."""
        ...

    def get_vnc_port(self) -> int | None:
        """Return VNC port for computer-use, or ``None`` if N/A."""
        ...

    def get_vnc_credentials(self) -> tuple[str, str] | None:
        """Return ``(username, password)`` for the VNC server, or ``None``.

        Used by the WebSocket proxy to authenticate against the guest's
        VNC server on behalf of clients that shouldn't see credentials
        (e.g. browser-side noVNC).  Backends with unauthenticated VNC
        servers — Linux ``wayvnc``, Docker computer-use — return ``None``.
        """
        ...

    def get_vnc_quirks(self) -> frozenset[VncQuirk]:
        """Return RFB-protocol workarounds the proxy must apply for this
        backend's VNC server.

        Default: empty — wayvnc, Apple Screen Sharing and QEMU's VNC are
        all standards-compliant.  Override only when the upstream
        violates the RFB protocol (e.g. crashes on ``SetEncodings``).
        """
        ...

    def get_text_input_state_path(self) -> Path | None:
        """Return host-side path to the text-input-state file, or ``None``."""
        ...

    def get_text_input_active(self) -> bool:
        """Return ``True`` if a text input field is focused in the sandbox."""
        ...

    def take_screenshot(self, output_path: Path) -> None:
        """Take a screenshot and save as PNG to *output_path*.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_click(self, x: int, y: int, button: str = "left") -> None:
        """Click at screen coordinates (*x*, *y*) with *button*.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_type(self, text: str) -> None:
        """Type *text* as keyboard input.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_key(self, key_str: str) -> None:
        """Press a key or combo (e.g. ``"ctrl+a"``) .

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def send_scroll(
        self, x: int, y: int, direction: str, amount: int = 3,
    ) -> None:
        """Scroll at screen coordinates (*x*, *y*).

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def focus_window(self, name: str) -> None:
        """Focus a window by name or title substring.

        Raises :class:`NotImplementedError` for backends without this capability.
        """
        ...

    def get_clipboard(self) -> str:
        """Get the Wayland clipboard contents via ``wl-paste``.

        Returns the clipboard text, or an empty string if unavailable.
        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    def set_clipboard(self, text: str) -> None:
        """Set the Wayland clipboard contents via ``wl-copy``.

        Raises :class:`NotImplementedError` for backends without computer-use.
        """
        ...

    async def copy_files_in(self, host_paths: list[Path]) -> list[Path]:
        """Copy files from the host into the sandbox.

        Returns a list of sandbox-side paths (same order/length as
        *host_paths*).  If a copy fails for a particular file, the
        original host path is kept as a fallback.

        For non-container backends where host and sandbox share a
        filesystem, this is a no-op that returns *host_paths* unchanged.
        """
        ...

    # -- Port forwarding ------------------------------------------------------

    def supports_port_forwarding(self) -> bool:
        """Return ``True`` if this backend supports runtime port forwarding.

        When ``False``, the ``port_forward`` MCP tool is not registered
        and the other methods on this section may raise
        :class:`NotImplementedError`.
        """
        ...

    def add_port_forward(
        self,
        guest_port: int,
        requested_host_port: int | None,
        scope_key: str | None,
        description: str | None,
    ) -> PortForward:
        """Open a TCP forward from *guest_port* in the sandbox to a host port.

        The host port is bound to ``127.0.0.1`` only.  If
        *requested_host_port* is taken (or ``None``), the system picks a
        free port and returns it on the resulting :class:`PortForward`.

        Raises :class:`NotImplementedError` for backends without support.
        """
        ...

    def remove_port_forward(self, forward_id: str) -> bool:
        """Tear down a previously created forward.  Returns ``True`` if removed."""
        ...

    def list_port_forwards(
        self, scope_key: str | None = None,
    ) -> list[PortForward]:
        """List active forwards, optionally filtered by *scope_key*."""
        ...

    def cleanup_port_forwards(self, scope_key: str | None = None) -> None:
        """Tear down all forwards, or just those owned by *scope_key*."""
        ...
