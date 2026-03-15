"""Cloudflared tunnel management for the review app.

Manages the lifecycle of a cloudflared quick tunnel, including
auto-downloading the binary if not found on the system.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import stat
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Directory where we download cloudflared if not found in $PATH.
_BIN_DIR = Path.home() / ".config" / "openudang" / "bin"

# GitHub release URL template for cloudflared binaries.
_DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# Map (system, machine) to the cloudflared binary name on GitHub releases.
_BINARY_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "cloudflared-linux-amd64",
    ("Linux", "aarch64"): "cloudflared-linux-arm64",
    ("Linux", "armv7l"): "cloudflared-linux-arm",
    ("Darwin", "x86_64"): "cloudflared-darwin-amd64.tgz",
    ("Darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
    ("Windows", "AMD64"): "cloudflared-windows-amd64.exe",
}


def _get_binary_name() -> str | None:
    """Return the cloudflared release binary name for this platform."""
    system = platform.system()
    machine = platform.machine()
    return _BINARY_MAP.get((system, machine))


def _find_cloudflared() -> str | None:
    """Find the cloudflared binary, checking our bin dir first, then $PATH."""
    # Check our managed bin dir.
    local_bin = _BIN_DIR / "cloudflared"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)

    # Check $PATH via `which`.
    import shutil

    path = shutil.which("cloudflared")
    if path:
        return path

    return None


async def _download_cloudflared() -> str:
    """Download the cloudflared binary for this platform.

    Returns the path to the downloaded binary.

    Raises:
        RuntimeError: If the platform is unsupported or download fails.
    """
    binary_name = _get_binary_name()
    if binary_name is None:
        raise RuntimeError(
            f"Unsupported platform for cloudflared auto-download: "
            f"{platform.system()} {platform.machine()}. "
            f"Please install cloudflared manually: "
            f"https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        )

    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = _BIN_DIR / "cloudflared"
    url = f"{_DOWNLOAD_BASE}/{binary_name}"

    logger.info("Downloading cloudflared from %s ...", url)

    if binary_name.endswith(".tgz"):
        # macOS ships as a tarball.
        await _download_and_extract_tgz(url, target)
    else:
        await _download_file(url, target)

    # Make executable.
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("cloudflared downloaded to %s", target)
    return str(target)


async def _download_file(url: str, dest: Path) -> None:
    """Download a file from a URL to a local path using httpx."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(".tmp")
            try:
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                tmp.rename(dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise


async def _download_and_extract_tgz(url: str, dest: Path) -> None:
    """Download a .tgz and extract the cloudflared binary from it."""
    import httpx
    import tarfile
    import tempfile

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name

    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            # Find the cloudflared binary in the archive.
            for member in tar.getmembers():
                if member.name.endswith("cloudflared") or member.name == "cloudflared":
                    f = tar.extractfile(member)
                    if f is not None:
                        with open(dest, "wb") as out:
                            out.write(f.read())
                        return
            raise RuntimeError(
                "cloudflared binary not found in downloaded archive"
            )
    finally:
        os.unlink(tmp_path)


async def ensure_cloudflared() -> str:
    """Ensure cloudflared is available, downloading if necessary.

    Returns the path to the cloudflared binary.

    Raises:
        RuntimeError: If cloudflared cannot be found or downloaded.
    """
    path = _find_cloudflared()
    if path:
        logger.info("Found cloudflared at %s", path)
        return path

    logger.info("cloudflared not found, attempting auto-download...")
    return await _download_cloudflared()


async def start_tunnel(
    port: int, cloudflared_path: str | None = None
) -> tuple[asyncio.subprocess.Process, str]:
    """Start a cloudflared quick tunnel pointing to the given port.

    Args:
        port: Local port the HTTP server is listening on.
        cloudflared_path: Path to cloudflared binary. If None, will be
            located/downloaded automatically.

    Returns:
        (process, public_url) — the subprocess handle and the assigned
        trycloudflare.com URL.

    Raises:
        RuntimeError: If cloudflared cannot be started or URL cannot be
            parsed from output.
    """
    if cloudflared_path is None:
        cloudflared_path = await ensure_cloudflared()

    logger.info(
        "Starting cloudflared tunnel to http://localhost:%d ...", port
    )

    proc = await asyncio.create_subprocess_exec(
        cloudflared_path,
        "tunnel",
        "--url",
        f"http://localhost:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # cloudflared prints the assigned URL to stderr.  We need to read
    # stderr lines until we find it, with a timeout.
    url = await _parse_tunnel_url(proc, timeout=30.0)

    logger.info("Cloudflared tunnel active: %s", url)
    return proc, url


async def _parse_tunnel_url(
    proc: asyncio.subprocess.Process, timeout: float = 30.0
) -> str:
    """Read cloudflared stderr until the tunnel URL appears.

    The URL line looks like:
        ... | https://xxx-yyy-zzz.trycloudflare.com ...

    Raises:
        RuntimeError: If the URL is not found within the timeout or the
            process exits prematurely.
    """
    import re

    url_pattern = re.compile(r"(https://[a-zA-Z0-9_-]+\.trycloudflare\.com)")

    assert proc.stderr is not None

    try:
        async with asyncio.timeout(timeout):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    # Process exited.
                    exit_code = await proc.wait()
                    raise RuntimeError(
                        f"cloudflared exited with code {exit_code} "
                        f"before printing a tunnel URL"
                    )
                decoded = line.decode("utf-8", errors="replace").strip()
                logger.debug("cloudflared: %s", decoded)

                match = url_pattern.search(decoded)
                if match:
                    return match.group(1)
    except TimeoutError:
        proc.terminate()
        raise RuntimeError(
            f"Timed out after {timeout}s waiting for cloudflared tunnel URL"
        )


async def stop_tunnel(proc: asyncio.subprocess.Process) -> None:
    """Gracefully stop a cloudflared tunnel process."""
    if proc.returncode is not None:
        return  # Already exited.

    logger.info("Stopping cloudflared tunnel...")
    proc.terminate()
    try:
        async with asyncio.timeout(10.0):
            await proc.wait()
    except TimeoutError:
        logger.warning("cloudflared did not exit cleanly, killing...")
        proc.kill()
        await proc.wait()

    logger.info("cloudflared tunnel stopped")
