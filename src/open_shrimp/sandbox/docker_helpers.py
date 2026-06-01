"""Docker container support for isolated OpenCode execution.

When a context is sandboxed, OpenCode runs inside a Docker container instead
of directly on the host.  This provides strong filesystem isolation — the
agent can only access the bind-mounted project directory and sandbox state.

Containers are **persistent**: one long-lived container per context name,
shared across all sessions and threads.  The first invocation starts the
container with ``docker run -d`` (using ``sleep infinity`` as the keep-alive
process), and subsequent operations use ``docker exec`` inside the
already-running container.

Testcontainers Ryuk is used as a crash-safety net: a TCP connection to Ryuk
acts as a liveness signal for the bot process.  If the bot dies without
graceful shutdown, Ryuk reaps all labelled containers after a short timeout.

"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from importlib.resources import files as _pkg_files
from pathlib import Path

from open_shrimp.paths import data_dir as _data_dir
from open_shrimp.sandbox.skill_paths import (
    SANDBOX_HOME,
    SANDBOX_TMP,
    existing_global_skill_dirs,
)

logger = logging.getLogger(__name__)

def _image_created_ts(image_name: str) -> str | None:
    """Return the creation timestamp of a Docker image, or None if missing."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_name,
         "--format", "{{.Created}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _container_image_id(container_name: str) -> str | None:
    """Return the image ID a running container was created from."""
    result = subprocess.run(
        ["docker", "inspect", container_name,
         "--format", "{{.Image}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _image_id(image_name: str) -> str | None:
    """Return the ID of a Docker image."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_name,
         "--format", "{{.Id}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# Docker image name used for containerized contexts.
CONTAINER_IMAGE = "openshrimp-opencode:latest"

# Docker image name for computer-use (GUI) contexts.
COMPUTER_USE_IMAGE = "openshrimp-computer-use:latest"

# Fixed in-container port for sandbox-owned OpenCode servers.
OPENCODE_GUEST_PORT = 4096

# Base directory for per-context container state (session storage, etc.).
def container_state_dir() -> Path:
    """Return the base directory for per-context Docker sandbox state."""
    return _data_dir() / "containers"


# Custom seccomp profile for DinD: Docker's default + keyctl (inner runc
# session keyrings) + pivot_root (inner container rootfs setup).
def _find_seccomp_profile() -> Path:
    """Locate the DinD seccomp profile.

    Tries the repo root first (dev/editable installs), then falls back to
    importlib.resources (installed wheels/PyApp).  The profile must be
    written to a real file on disk because ``docker run --security-opt
    seccomp=`` requires a filesystem path.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    repo_profile = repo_root / "seccomp-dind.json"
    if repo_profile.is_file():
        return repo_profile

    # Installed wheel / PyApp — extract via importlib.resources.
    pkg_profile = _pkg_files("open_shrimp").joinpath("seccomp-dind.json")
    # importlib.resources may return a MultiplexedPath or similar; we need
    # a real filesystem path for docker's --security-opt.
    if hasattr(pkg_profile, "is_file") and pkg_profile.is_file():
        return Path(str(pkg_profile))

    # As a last resort try importlib.resources.as_file for zip-backed resources.
    from importlib.resources import as_file
    with as_file(pkg_profile) as p:
        # Copy to a persistent temp location so docker can read it after
        # the context manager exits.
        persistent = Path(tempfile.gettempdir()) / "openshrimp-seccomp-dind.json"
        if not persistent.exists():
            shutil.copy2(p, persistent)
        return persistent


def ensure_image(
    image_name: str = CONTAINER_IMAGE,
    dockerfile: str | None = None,
    base_image: str | None = None,
    log_file: Path | None = None,
) -> None:
    """Ensure the container image exists, building it if necessary.

    When *dockerfile* is ``None`` (the default), builds the base image from
    the bundled ``Dockerfile.opencode``. When a custom *dockerfile* path is
    provided, builds from that file instead.

    Args:
        image_name: Docker image tag to build/check.
        dockerfile: Optional path to a custom Dockerfile.  When set,
            the build context is the directory containing the
            Dockerfile (so ``COPY`` instructions work relative to it).
        base_image: When set with a custom *dockerfile*, ensure this
            image exists (instead of the default base) before building.
            Useful for layering a custom Dockerfile on top of the
            computer-use image.

    Raises:
        RuntimeError: If the OpenCode binary cannot be found or if the Docker
            build fails.
    """
    image_exists = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
    ).returncode == 0

    # Check if the base image is newer than the derived image.
    needs_rebuild = False
    if image_exists and dockerfile is not None:
        effective_base = base_image or (
            CONTAINER_IMAGE if image_name != CONTAINER_IMAGE else None
        )
        if effective_base:
            base_ts = _image_created_ts(effective_base)
            derived_ts = _image_created_ts(image_name)
            if base_ts and derived_ts and base_ts > derived_ts:
                logger.info(
                    "Base image %s (%s) is newer than %s (%s), rebuilding",
                    effective_base, base_ts, image_name, derived_ts,
                )
                needs_rebuild = True

    if image_exists and not needs_rebuild:
        logger.info("Container image %s already exists", image_name)
        return

    if needs_rebuild:
        logger.info("Rebuilding container image %s...", image_name)
    else:
        logger.info("Container image %s not found, building...", image_name)

    opencode_binary = _find_opencode_binary()
    logger.info("Using OpenCode binary: %s", opencode_binary)

    if dockerfile is not None:
        # Ensure the base image exists before building a custom image
        # that likely depends on it (e.g. FROM openshrimp-opencode:latest).
        if base_image:
            # Caller explicitly specified which base to ensure (e.g.
            # the computer-use image).  That base's own dependencies
            # should already be satisfied by the caller.
            subprocess.run(
                ["docker", "image", "inspect", base_image],
                capture_output=True,
                check=True,
            )
        elif image_name != CONTAINER_IMAGE:
            ensure_image(image_name=CONTAINER_IMAGE, dockerfile=None)

        # Custom Dockerfile: use its parent directory as the build context,
        # copying the OpenCode binary in alongside it.
        dockerfile_path = Path(dockerfile).resolve()
        if not dockerfile_path.is_file():
            raise RuntimeError(
                f"Custom Dockerfile not found: {dockerfile_path}"
            )
        build_dir_path = dockerfile_path.parent
        # Copy OpenCode binary into the build context (if not already there).
        opencode_dest = build_dir_path / "opencode"
        if not opencode_dest.exists() or not opencode_dest.samefile(Path(opencode_binary)):
            shutil.copy2(opencode_binary, opencode_dest)
        extra_args = None
        if base_image:
            extra_args = ["--build-arg", f"BASE_IMAGE={base_image}"]
        _docker_build(
            image_name=image_name,
            build_dir=str(build_dir_path),
            dockerfile_name=dockerfile_path.name,
            extra_build_args=extra_args,
            log_file=log_file,
        )
    else:
        # Default: bundled Dockerfile.opencode in a temp build context.
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        repo_dockerfile = repo_root / "Dockerfile.opencode"
        if repo_dockerfile.is_file():
            dockerfile_text = repo_dockerfile.read_text(encoding="utf-8")
        else:
            dockerfile_text = (
                _pkg_files("open_shrimp")
                .joinpath("Dockerfile.opencode")
                .read_text(encoding="utf-8")
            )

        with tempfile.TemporaryDirectory(
            prefix="openshrimp-build-"
        ) as build_dir:
            build_path = Path(build_dir)
            shutil.copy2(_find_opencode_binary(), build_path / "opencode")
            (build_path / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
            _docker_build(
                image_name=image_name,
                build_dir=build_dir,
                log_file=log_file,
            )

    logger.info("Successfully built container image %s", image_name)


def ensure_computer_use_image(
    image_name: str = COMPUTER_USE_IMAGE,
    log_file: Path | None = None,
) -> None:
    """Ensure the computer-use container image exists, building if necessary.

    Builds the base image first (if needed), then
    layers ``Dockerfile.computer-use`` on top with labwc, wlrctl, grim,
    wayvnc, and Chromium.
    """
    image_exists = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
    ).returncode == 0

    # Check if the base image is newer than the computer-use image.
    needs_rebuild = False
    if image_exists:
        base_ts = _image_created_ts(CONTAINER_IMAGE)
        derived_ts = _image_created_ts(image_name)
        if base_ts and derived_ts and base_ts > derived_ts:
            logger.info(
                "Base image %s (%s) is newer than %s (%s), rebuilding",
                CONTAINER_IMAGE, base_ts, image_name, derived_ts,
            )
            needs_rebuild = True

    if image_exists and not needs_rebuild:
        logger.info("Computer-use image %s already exists", image_name)
        return

    # Ensure the base image exists first.
    ensure_image(image_name=CONTAINER_IMAGE, dockerfile=None, log_file=log_file)

    if needs_rebuild:
        logger.info("Rebuilding computer-use image %s...", image_name)
    else:
        logger.info("Computer-use image %s not found, building...", image_name)

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    repo_dockerfile = repo_root / "Dockerfile.computer-use"
    computer_use_dir = repo_root / "computer-use"

    if repo_dockerfile.is_file() and computer_use_dir.is_dir():
        # Build from the repo root so COPY computer-use/* works.
        _docker_build(
            image_name=image_name,
            build_dir=str(repo_root),
            dockerfile_name="Dockerfile.computer-use",
            log_file=log_file,
        )
    else:
        # Installed wheel / PyApp — extract assets to a temp dir.
        with tempfile.TemporaryDirectory(
            prefix="openshrimp-computer-use-build-"
        ) as build_dir:
            build_path = Path(build_dir)
            pkg = _pkg_files("open_shrimp")

            # Copy Dockerfile.
            dockerfile_text = pkg.joinpath(
                "Dockerfile.computer-use"
            ).read_text(encoding="utf-8")
            (build_path / "Dockerfile.computer-use").write_text(
                dockerfile_text, encoding="utf-8",
            )

            # Copy computer-use assets.
            cu_dir = build_path / "computer-use"
            cu_dir.mkdir()
            for asset_name in (
                "entrypoint.sh",
                "rc.xml",
                "autostart",
                "seat-keyboard.c",
                "virtual-keyboard-unstable-v1.xml",
                "input-method-unstable-v2.xml",
            ):
                asset = pkg.joinpath("computer-use", asset_name)
                (cu_dir / asset_name).write_text(
                    asset.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )

            _docker_build(
                image_name=image_name,
                build_dir=str(build_path),
                dockerfile_name="Dockerfile.computer-use",
                log_file=log_file,
            )

    logger.info("Successfully built computer-use image %s", image_name)


def _docker_build(
    image_name: str,
    build_dir: str,
    dockerfile_name: str = "Dockerfile",
    extra_build_args: list[str] | None = None,
    log_file: Path | None = None,
) -> None:
    """Run ``docker build`` and stream output to the logger.

    Args:
        log_file: Optional path to a file where build output is also
            written line-by-line (with flush) for the terminal mini app.

    Raises:
        RuntimeError: If the build fails.
    """
    cmd = [
        "docker", "build",
        "-t", image_name,
        "-f", dockerfile_name,
        "--build-arg", "OPENCODE_BIN=opencode",
    ]
    if extra_build_args:
        cmd.extend(extra_build_args)
    cmd.append(".")
    process = subprocess.Popen(
        cmd,
        cwd=build_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines: list[str] = []
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            logger.info("docker build: %s", line)
            if log_fh is not None:
                log_fh.write(line + "\n")
                log_fh.flush()
        returncode = process.wait()
    finally:
        if log_fh is not None:
            log_fh.close()

    if returncode != 0:
        output = "\n".join(output_lines)
        raise RuntimeError(
            f"Failed to build container image {image_name}. "
            f"Docker build output:\n{output}"
        )


def _ensure_state_dir(context_name: str) -> Path:
    """Create and return the container state directory for a context.

    This directory holds per-context sandbox state.
    """
    state_dir = container_state_dir() / context_name
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_opencode_home_dir(context_name: str) -> Path:
    """Return the host-side OpenCode state directory for a context."""
    path = _ensure_state_dir(context_name) / "opencode-home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find_opencode_binary() -> str:
    env_bin = os.environ.get("OPENCODE_BIN")
    if env_bin and Path(env_bin).is_file():
        return env_bin
    home_bin = Path.home() / ".opencode" / "bin" / "opencode"
    if home_bin.is_file():
        return str(home_bin)
    which = shutil.which("opencode")
    if which:
        return which
    raise RuntimeError(
        "Could not find the `opencode` binary for the sandbox image. "
        "Set OPENCODE_BIN or install it at ~/.opencode/bin/opencode."
    )


def get_opencode_host_port(context_name: str) -> int | None:
    """Return the host-mapped OpenCode port for a container, or None."""
    name = _container_name(context_name)
    result = subprocess.run(
        ["docker", "port", name, str(OPENCODE_GUEST_PORT)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().splitlines():
        port_str = line.rsplit(":", 1)[-1]
        try:
            return int(port_str)
        except ValueError:
            continue
    return None


def check_docker_available() -> bool:
    """Return True if Docker is available on the host."""
    return shutil.which("docker") is not None


def get_screenshots_dir(context_name: str) -> Path:
    """Return the host-side screenshots directory for a computer-use context."""
    return container_state_dir() / context_name / "screenshots"


def get_text_input_state_path(context_name: str) -> Path:
    """Return the host-side text-input-state file for a computer-use context."""
    return container_state_dir() / context_name / "text-input-state"


def get_text_input_active(context_name: str) -> bool:
    """Check if a text input field is focused inside a computer-use container.

    Reads the bind-mounted text-input-state file written by seat-keyboard's
    input-method-v2 monitor.  Returns True if active, False otherwise.
    """
    path = get_text_input_state_path(context_name)
    try:
        return path.read_text(encoding="utf-8").strip() == "1"
    except (FileNotFoundError, OSError):
        return False


def get_vnc_port(context_name: str) -> int | None:
    """Return the host-mapped VNC port for a computer-use container, or None.

    The computer-use container exposes port 5900 with dynamic host mapping.
    This function queries Docker for the actual mapped host port.
    """
    name = _container_name(context_name)
    result = subprocess.run(
        ["docker", "port", name, "5900"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    # Output is like "0.0.0.0:32768" or "[::]:32768\n0.0.0.0:32768".
    for line in result.stdout.strip().splitlines():
        port_str = line.rsplit(":", 1)[-1]
        try:
            return int(port_str)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Ryuk reaper — crash-safe container cleanup
# ---------------------------------------------------------------------------

RYUK_IMAGE = "testcontainers/ryuk:0.11.0"

# These globals are set by ``DockerSandboxManager.set_instance_prefix()``
# so that the free functions below (called by ``DockerSandbox``) see the
# correct prefix/label.  New code should access these values through the
# manager instead.
_CONTAINER_LABEL = "openshrimp"
_INSTANCE_PREFIX = "openshrimp"



# ---------------------------------------------------------------------------
# Persistent container lifecycle
# ---------------------------------------------------------------------------

def container_name(context_name: str) -> str:
    """Return the fixed Docker container name for a context."""
    return f"{_INSTANCE_PREFIX}-{context_name}"


# Keep private alias for internal callers.
_container_name = container_name


def _get_container_state(name: str) -> str | None:
    """Return the container state ('running', 'exited', …) or None."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()



# Shell script that starts rootless Docker daemon inside the container,
# waits for it to be ready, then keeps the container alive.
_DIND_ENTRYPOINT = r"""#!/bin/bash
set -eu

# Ensure the current uid exists in /etc/passwd — rootless Docker's
# newuidmap/newgidmap require a valid passwd entry.
MY_UID=$(id -u)
MY_GID=$(id -g)
if ! getent passwd "$MY_UID" > /dev/null 2>&1; then
    echo "openshrimp:x:${MY_UID}:${MY_GID}::/home/openshrimp:/bin/bash" >> /etc/passwd
fi
if ! getent group "$MY_GID" > /dev/null 2>&1; then
    echo "openshrimp:x:${MY_GID}:" >> /etc/group
fi

# Register subordinate uid/gid ranges for the current (non-root) user.
echo "openshrimp:100000:65536" > /etc/subuid
echo "openshrimp:100000:65536" > /etc/subgid

# XDG_RUNTIME_DIR is required by rootless dockerd.  It must be outside
# /run because rootlesskit's --copy-up=/run overlays /run with a tmpfs
# inside its namespace, shadowing anything the outer namespace writes there.
export XDG_RUNTIME_DIR="/tmp/runtime-${MY_UID}"
mkdir -p "$XDG_RUNTIME_DIR"

# Patch dockerd-rootless.sh to tolerate sysctl failures (ip_forward is
# already set via the container's --sysctl flag).
sed 's/sysctl -w \(.*\)$/sysctl -w \1 || true/' /usr/bin/dockerd-rootless.sh \
    > /tmp/dockerd-rootless.sh
chmod +x /tmp/dockerd-rootless.sh

# Disable slirp4netns's internal sandbox and seccomp — these try to
# create mount namespaces/apply seccomp filters which are blocked by the
# outer container's security profile.  The outer container already
# provides isolation.
export DOCKERD_ROOTLESS_ROOTLESSKIT_SLIRP4NETNS_SANDBOX=false
export DOCKERD_ROOTLESS_ROOTLESSKIT_SLIRP4NETNS_SECCOMP=false

# Start rootless Docker daemon (no iptables in nested containers).
SKIP_IPTABLES=1 /tmp/dockerd-rootless.sh --iptables=false \
    > /tmp/dockerd.log 2>&1 &

# Wait for Docker to be ready (up to 30s).
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"
for _i in $(seq 1 30); do
    if docker info > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Symlink the rootless socket to the standard path so that tools like
# Testcontainers/Ryuk find the daemon at /var/run/docker.sock without
# needing DOCKER_HOST.
mkdir -p /var/run
ln -sf "${XDG_RUNTIME_DIR}/docker.sock" /var/run/docker.sock

# Also create a Docker context so that `docker exec` sessions (which don't
# inherit runtime env vars or the symlink's XDG target) can find the daemon.
docker context create rootless --docker "host=unix://${XDG_RUNTIME_DIR}/docker.sock" 2>/dev/null || true
docker context use rootless 2>/dev/null || true

# Add masquerade rules for container outbound networking.
# rootless dockerd runs with --iptables=false (required in nested containers),
# so we must manually add NAT rules for all bridge subnets (docker0 + any
# docker-compose br-* networks created later).  Run in a background loop so
# dynamically-created networks (e.g. docker compose up) get rules too.
_nsenter() {
    CHILD_PID=$(cat "${XDG_RUNTIME_DIR}/dockerd-rootless/child_pid" 2>/dev/null)
    [ -z "$CHILD_PID" ] && return 1
    nsenter --preserve-credentials -U -n -t "$CHILD_PID" "$@"
}

ensure_masquerade() {
    _nsenter true 2>/dev/null || return
    for BRIDGE in $(_nsenter ip -o link show type bridge 2>/dev/null \
            | grep -oP '(?<=: )\S+(?=:)'); do
        SUBNET=$(_nsenter ip -4 addr show "$BRIDGE" 2>/dev/null \
            | grep -oP 'inet \K[\d./]+')
        if [ -n "$SUBNET" ]; then
            _nsenter iptables -t nat -C POSTROUTING \
                -s "$SUBNET" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || \
            _nsenter iptables -t nat -A POSTROUTING \
                -s "$SUBNET" ! -o "$BRIDGE" -j MASQUERADE 2>/dev/null || true
        fi
    done
}

cleanup_masquerade() {
    _nsenter true 2>/dev/null || return
    BRIDGES=$(_nsenter ip -o link show type bridge 2>/dev/null \
        | grep -oP '(?<=: )\S+(?=:)')
    # Walk POSTROUTING rules; delete any whose output bridge no longer exists.
    _nsenter iptables -t nat -S POSTROUTING 2>/dev/null \
        | grep 'MASQUERADE' | while read -r RULE; do
        RULE_BRIDGE=$(echo "$RULE" | sed -n 's/.* -o \([^ ]*\).*/\1/p')
        RULE_SUBNET=$(echo "$RULE" | sed -n 's/.*-s \([^ ]*\).*/\1/p')
        [ -z "$RULE_BRIDGE" ] || [ -z "$RULE_SUBNET" ] && continue
        if ! echo "$BRIDGES" | grep -qxF "$RULE_BRIDGE"; then
            _nsenter iptables -t nat -D POSTROUTING \
                -s "$RULE_SUBNET" ! -o "$RULE_BRIDGE" -j MASQUERADE \
                2>/dev/null || true
        fi
    done
}

# Run once immediately, then react to network events via docker events.
ensure_masquerade
docker events --filter type=network --format '{{.Action}}' 2>/dev/null | \
    while read -r event; do
        case "$event" in
            create|connect) ensure_masquerade ;;
            destroy) cleanup_masquerade ;;
        esac
    done &

# Keep the container alive.  CLI invocations arrive via `docker exec`.
exec sleep infinity
"""


def _build_docker_run_argv(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str = CONTAINER_IMAGE,
) -> tuple[list[str], str]:
    """Build the ``docker run -d`` argv for creating a persistent container.

    Returns ``(docker_run_argv, container_name)`` where *docker_run_argv*
    is a complete argv list ending with the image and keep-alive command.
    """
    state_dir = _ensure_state_dir(context_name)
    uid = os.getuid()
    gid = os.getgid()
    container_name = _container_name(context_name)

    # Git identity — baked into the container at creation time.
    git_env_args: list[str] = []
    for git_key, env_vars in [
        ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
        ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
    ]:
        try:
            value = subprocess.check_output(
                ["git", "config", "--global", git_key],
                text=True,
            ).strip()
            if value:
                for env_var in env_vars:
                    git_env_args.append(f"{env_var}={value}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    docker_argv: list[str] = [
        "docker", "run", "-d",
        "--name", container_name,
        "--label", f"{_CONTAINER_LABEL}=true",
        "--label", f"{_CONTAINER_LABEL}.context={context_name}",
        "--user", f"{uid}:{gid}",
        "-e", f"HOME={SANDBOX_HOME}",
    ]
    for env_arg in git_env_args:
        docker_argv.extend(["-e", env_arg])

    # Ensure host.docker.internal resolves inside the container.
    # This is automatic on Docker Desktop but needs --add-host on
    # native/rootless Docker (Linux).  The MCP proxy listens on the
    # host and sandboxed contexts reach it via this hostname.
    docker_argv.extend(["--add-host", "host.docker.internal:host-gateway"])

    # Mount task-output tmp dir inside the container so background task
    # outputs are written to the host-visible state directory.
    task_tmp_dir = state_dir / "tmp"
    task_tmp_dir.mkdir(exist_ok=True)
    opencode_home = get_opencode_home_dir(context_name)
    docker_argv.extend([
        "-v", f"{project_dir}:{project_dir}",
        "-v", f"{opencode_home}:{SANDBOX_HOME}/.local/share/opencode",
        "-v", f"{task_tmp_dir}:{SANDBOX_TMP.replace('1000', str(uid))}",
    ])

    # Expose the sandbox-owned OpenCode server to the host via loopback.
    docker_argv.extend(["-p", f"127.0.0.1::{OPENCODE_GUEST_PORT}"])
    # Sub-mount on top of state_dir; Docker resolves nested binds in flag order.
    for host_skills, guest_skills in existing_global_skill_dirs():
        docker_argv.extend([
            "--mount", f"type=bind,source={host_skills},target={guest_skills},readonly",
        ])
    for extra_dir in additional_directories or []:
        docker_argv.extend(["-v", f"{extra_dir}:{extra_dir}"])

    if docker_in_docker:
        docker_argv.extend([
            "--cap-add", "SYS_ADMIN",
            "--security-opt", "apparmor=unconfined",
            "--security-opt", "systempaths=unconfined",
            "--security-opt", f"seccomp={_find_seccomp_profile()}",
            "--device", "/dev/net/tun",
            "--sysctl", "net.ipv4.ip_forward=1",
        ])
        docker_data_dir = state_dir / "docker-data"
        docker_data_dir.mkdir(exist_ok=True)
        docker_argv.extend([
            "-v", f"{docker_data_dir}:{SANDBOX_HOME}/.local/share/docker",
        ])
        if not computer_use:
            # Standalone DinD: use the dedicated entrypoint script.
            entrypoint_path = state_dir / "dind-entrypoint.sh"
            entrypoint_path.write_text(_DIND_ENTRYPOINT, encoding="utf-8")
            entrypoint_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                                  | stat.S_IROTH | stat.S_IXOTH)
            docker_argv.extend([
                "-v",
                f"{entrypoint_path}:/usr/local/bin/dind-entrypoint.sh:ro",
            ])

    if computer_use:
        # Headless Wayland compositor environment.
        docker_argv.extend([
            "-e", "WLR_BACKENDS=headless",
            "-e", "WLR_RENDERER=pixman",
            "-e", "WLR_HEADLESS_OUTPUTS=1",
            "-e", "WAYLAND_DISPLAY=wayland-0",
            "-e", f"XDG_RUNTIME_DIR=/tmp/runtime-{uid}",
        ])
        # Bind-mount screenshots directory for host access.
        screenshots_dir = state_dir / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)
        docker_argv.extend([
            "-v", f"{screenshots_dir}:/tmp/screenshots",
        ])
        # Bind-mount text-input-state file for host-side inotify watching.
        # seat-keyboard writes "1"/"0" here when text fields gain/lose focus.
        text_input_state_file = state_dir / "text-input-state"
        text_input_state_file.touch()
        docker_argv.extend([
            "-v", f"{text_input_state_file}:/tmp/text-input-state",
        ])
        # Expose VNC port (dynamic mapping to avoid conflicts).
        docker_argv.extend(["-p", "127.0.0.1::5900"])
        # When both computer_use and DinD are enabled, the computer-use
        # entrypoint handles dockerd startup via ENABLE_DIND=1.
        if docker_in_docker:
            docker_argv.extend(["-e", "ENABLE_DIND=1"])

    docker_argv.extend(["-w", project_dir])

    # Image and keep-alive command.
    docker_argv.append(image_name)
    if computer_use:
        # The computer-use entrypoint handles both compositor and
        # optional DinD (via ENABLE_DIND env var).
        docker_argv.append("/usr/local/bin/computer-use-entrypoint.sh")
    elif docker_in_docker:
        docker_argv.append("/usr/local/bin/dind-entrypoint.sh")
    else:
        docker_argv.extend(["sleep", "infinity"])

    return docker_argv, container_name


def ensure_container_running(
    context_name: str,
    project_dir: str,
    additional_directories: list[str] | None = None,
    docker_in_docker: bool = False,
    computer_use: bool = False,
    image_name: str = CONTAINER_IMAGE,
) -> str:
    """Ensure a persistent container is running for the given context.

    If the container already exists and is running, this is a fast no-op
    (a single ``docker inspect``).  Otherwise it creates a new detached
    container.

    Race-safe: if two threads try to create the container simultaneously,
    one will get a name-conflict error and fall through to the running
    container.

    Returns:
        The container name (e.g. ``openshrimp-dev``).
    """
    name = _container_name(context_name)
    state = _get_container_state(name)
    if state == "running":
        # Check if the container's image matches the current image tag.
        container_img = _container_image_id(name)
        current_img = _image_id(image_name)
        if container_img and current_img and container_img != current_img:
            logger.info(
                "Container %s is running an outdated image, recreating",
                name,
            )
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True,
            )
        else:
            logger.info("Container %s already running", name)
            return name
    elif state is not None:
        # Remove stale container (exited, dead, created).
        logger.info("Removing stale container %s (state=%s)", name, state)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    docker_argv, _ = _build_docker_run_argv(
        context_name=context_name,
        project_dir=project_dir,
        additional_directories=additional_directories,
        docker_in_docker=docker_in_docker,
        computer_use=computer_use,
        image_name=image_name,
    )

    result = subprocess.run(docker_argv, capture_output=True, text=True)
    if result.returncode != 0:
        # Race: another invocation may have created it.
        if _get_container_state(name) == "running":
            logger.info("Container %s started by another invocation", name)
            return name
        raise RuntimeError(
            f"Failed to start container {name}: {result.stderr.strip()}"
        )

    logger.info("Started persistent container %s", name)

    # For DinD, wait for the inner Docker daemon to be ready.
    if docker_in_docker:
        _wait_for_dind(name)

    # For computer-use, wait for the Wayland compositor.
    if computer_use:
        _wait_for_compositor(name)

    return name


def _wait_for_dind(container_name: str, timeout: int = 30) -> None:
    """Wait for the rootless Docker daemon inside a DinD container."""
    import time

    for i in range(timeout):
        result = subprocess.run(
            [
                "docker", "exec",
                container_name,
                "docker", "info",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("DinD ready in container %s after %ds", container_name, i)
            return
        time.sleep(1)
    logger.warning(
        "DinD not ready in container %s after %ds", container_name, timeout
    )


def _wait_for_compositor(container_name: str, timeout: int = 15) -> None:
    """Wait for labwc to create the Wayland socket inside the container."""
    import time

    uid = os.getuid()
    wayland_socket = f"/tmp/runtime-{uid}/wayland-0"
    for i in range(timeout * 5):  # Check every 0.2s
        result = subprocess.run(
            ["docker", "exec", container_name, "test", "-S", wayland_socket],
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info(
                "Compositor ready in container %s after %.1fs",
                container_name,
                i * 0.2,
            )
            return
        time.sleep(0.2)
    logger.warning(
        "Compositor not ready in container %s after %ds",
        container_name,
        timeout,
    )
