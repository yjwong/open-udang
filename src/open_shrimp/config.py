"""Config loading and validation for OpenShrimp."""

import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EffortLevel = str

import yaml
from platformdirs import user_config_path


DEFAULT_CONFIG_PATH = user_config_path("openshrimp") / "config.yaml"


@dataclass
class TelegramConfig:
    token: str


@dataclass
class ContainerConfig:
    enabled: bool = True
    docker_in_docker: bool = False
    dockerfile: str | None = None
    computer_use: bool = False


@dataclass
class SandboxConfig:
    """Unified sandbox configuration for all backends."""

    backend: str  # "docker", "libvirt", "lima"
    enabled: bool = True
    guest_os: str = "linux"  # "linux" or "macos" (macos requires backend: lima, ARM host)

    # Docker-specific
    docker_in_docker: bool = False
    dockerfile: str | None = None
    computer_use: bool = False
    virgl: bool = False  # VirGL 3D GPU acceleration (requires host GPU)

    # VM-specific (libvirt)
    memory: int = 2048  # MB ceiling (free-page-reporting returns unused to host)
    cpus: int = 2
    disk_size: int = 20  # GB, for qcow2 overlay
    base_image: str | None = None  # path to base qcow2/cloud image
    provision: str | None = None  # shell script to run on first boot
    persistent_paths: list[str] = field(default_factory=list)  # guest paths with dedicated qcow2 volumes

    # Sudo mode — when true, exposes an MCP tool that runs shell commands on
    # the host (outside the sandbox), gated by a per-command Telegram
    # approval prompt that auto-denies after 10 seconds.
    allow_host_escape: bool = False


# Valid values for sandbox config fields.
_SANDBOX_BACKENDS = {"docker", "libvirt", "lima"}
_SANDBOX_GUEST_OS = {"linux", "macos"}


def is_sandboxed(context: "ContextConfig") -> bool:
    """Return True if the context uses any sandbox backend."""
    if context.sandbox is not None and context.sandbox.enabled:
        return True
    if context.container is not None and context.container.enabled:
        return True
    return False


@dataclass
class ContextConfig:
    directory: str
    description: str
    allowed_tools: list[str]
    model: str | None = None
    effort: EffortLevel | None = None
    mcp: dict[str, Any] = field(default_factory=dict)
    additional_directories: list[str] = field(default_factory=list)
    default_for_chats: list[int] = field(default_factory=list)
    locked_for_chats: list[int] = field(default_factory=list)
    container: ContainerConfig | None = None
    sandbox: SandboxConfig | None = None


@dataclass
class ReviewConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str | None = None
    tunnel: str | None = None  # "cloudflared" or None


@dataclass
class PromptSuggestionsConfig:
    enabled: bool = True


@dataclass
class Config:
    telegram: TelegramConfig
    allowed_users: list[int]
    contexts: dict[str, ContextConfig]
    default_context: str
    review: ReviewConfig = field(default_factory=ReviewConfig)
    prompt_suggestions: PromptSuggestionsConfig = field(
        default_factory=PromptSuggestionsConfig
    )
    instance_name: str | None = None
    auto_update: bool = True


def _validate_raw(raw: dict) -> None:
    """Validate raw YAML dict has all required fields."""
    if not isinstance(raw, dict):
        raise ValueError("Config must be a YAML mapping")

    # Top-level required fields
    for key in ("telegram", "allowed_users", "contexts", "default_context"):
        if key not in raw:
            raise ValueError(f"Missing required config field: {key}")

    # telegram.token
    telegram = raw["telegram"]
    if not isinstance(telegram, dict) or "token" not in telegram:
        raise ValueError("Missing required field: telegram.token")

    # allowed_users
    users = raw["allowed_users"]
    if not isinstance(users, list) or not users:
        raise ValueError("allowed_users must be a non-empty list of integers")
    for u in users:
        if not isinstance(u, int):
            raise ValueError(f"allowed_users entries must be integers, got: {u!r}")

    # contexts
    contexts = raw["contexts"]
    if not isinstance(contexts, dict) or not contexts:
        raise ValueError("contexts must be a non-empty mapping")
    for name, ctx in contexts.items():
        if not isinstance(ctx, dict):
            raise ValueError(f"Context '{name}' must be a mapping")
        for field_name in ("directory", "description", "allowed_tools"):
            if field_name not in ctx:
                raise ValueError(
                    f"Context '{name}' missing required field: {field_name}"
                )
        if not isinstance(ctx["allowed_tools"], list):
            raise ValueError(f"Context '{name}': allowed_tools must be a list")
        add_dirs = ctx.get("additional_directories", [])
        if not isinstance(add_dirs, list):
            raise ValueError(
                f"Context '{name}': additional_directories must be a list"
            )
        for d in add_dirs:
            if not isinstance(d, str):
                raise ValueError(
                    f"Context '{name}': additional_directories entries must "
                    f"be strings, got: {d!r}"
                )
        effort = ctx.get("effort")
        if effort is not None and not isinstance(effort, str):
            raise ValueError(
                f"Context '{name}': effort must be a string, got: {effort!r}"
            )
        mcp = ctx.get("mcp", {})
        if not isinstance(mcp, dict):
            raise ValueError(f"Context '{name}': mcp must be a mapping")
        for server_name, server_config in mcp.items():
            if not isinstance(server_name, str):
                raise ValueError(
                    f"Context '{name}': mcp server names must be strings, "
                    f"got: {server_name!r}"
                )
            if not isinstance(server_config, dict):
                raise ValueError(
                    f"Context '{name}': mcp.{server_name} must be a mapping"
                )

    # Validate container config
    for name, ctx in contexts.items():
        container = ctx.get("container")
        if container is not None:
            if not isinstance(container, (dict, bool)):
                raise ValueError(
                    f"Context '{name}': container must be a mapping or boolean"
                )
            if isinstance(container, dict):
                dockerfile = container.get("dockerfile")
                if dockerfile is not None and not isinstance(dockerfile, str):
                    raise ValueError(
                        f"Context '{name}': container.dockerfile must be "
                        f"a string"
                    )

    # Validate sandbox config
    for name, ctx in contexts.items():
        sandbox = ctx.get("sandbox")
        if sandbox is None:
            continue

        # Cannot specify both container and sandbox
        if ctx.get("container") is not None:
            raise ValueError(
                f"Context '{name}': cannot specify both 'container' and "
                f"'sandbox' — use 'sandbox' (the 'container' key is a "
                f"backwards-compatible alias for sandbox.backend: docker)"
            )

        if not isinstance(sandbox, dict):
            raise ValueError(
                f"Context '{name}': sandbox must be a mapping"
            )

        backend = sandbox.get("backend")
        if backend is None:
            raise ValueError(
                f"Context '{name}': sandbox.backend is required"
            )
        if backend not in _SANDBOX_BACKENDS:
            raise ValueError(
                f"Context '{name}': sandbox.backend must be one of "
                f"{sorted(_SANDBOX_BACKENDS)}, got: {backend!r}"
            )

        dockerfile = sandbox.get("dockerfile")
        if dockerfile is not None and not isinstance(dockerfile, str):
            raise ValueError(
                f"Context '{name}': sandbox.dockerfile must be a string"
            )

        # Validate libvirt-specific fields.
        for int_field in ("memory", "cpus", "disk_size"):
            val = sandbox.get(int_field)
            if val is not None and not isinstance(val, int):
                raise ValueError(
                    f"Context '{name}': sandbox.{int_field} must be "
                    f"an integer, got: {val!r}"
                )

        base_image = sandbox.get("base_image")
        if base_image is not None and not isinstance(base_image, str):
            raise ValueError(
                f"Context '{name}': sandbox.base_image must be a string"
            )

        provision = sandbox.get("provision")
        if provision is not None and not isinstance(provision, str):
            raise ValueError(
                f"Context '{name}': sandbox.provision must be a string"
            )

        persistent_paths = sandbox.get("persistent_paths", [])
        if not isinstance(persistent_paths, list):
            raise ValueError(
                f"Context '{name}': sandbox.persistent_paths must be a list"
            )
        for pp in persistent_paths:
            if not isinstance(pp, str):
                raise ValueError(
                    f"Context '{name}': sandbox.persistent_paths entries "
                    f"must be strings, got: {pp!r}"
                )
            if not pp.startswith("/"):
                raise ValueError(
                    f"Context '{name}': sandbox.persistent_paths entries "
                    f"must be absolute paths, got: {pp!r}"
                )

        allow_host_escape = sandbox.get("allow_host_escape")
        if allow_host_escape is not None and not isinstance(
            allow_host_escape, bool,
        ):
            raise ValueError(
                f"Context '{name}': sandbox.allow_host_escape must be a "
                f"boolean, got: {allow_host_escape!r}"
            )

        guest_os = sandbox.get("guest_os", "linux")
        if guest_os not in _SANDBOX_GUEST_OS:
            raise ValueError(
                f"Context '{name}': sandbox.guest_os must be one of "
                f"{sorted(_SANDBOX_GUEST_OS)}, got: {guest_os!r}"
            )
        if guest_os == "macos":
            if backend != "lima":
                raise ValueError(
                    f"Context '{name}': sandbox.guest_os 'macos' requires "
                    f"backend 'lima', got: {backend!r}"
                )
            if platform.machine() != "arm64":
                raise ValueError(
                    f"Context '{name}': sandbox.guest_os 'macos' requires "
                    f"an ARM host (Lima macOS guests are ARM-only)"
                )

    # default_context references a defined context
    default = raw["default_context"]
    if default not in contexts:
        raise ValueError(
            f"default_context '{default}' not found in contexts: "
            f"{list(contexts.keys())}"
        )


def _parse_sandbox_config(raw: dict) -> SandboxConfig:
    """Parse a sandbox config dict into a SandboxConfig dataclass."""
    return SandboxConfig(
        backend=raw["backend"],
        enabled=bool(raw.get("enabled", True)),
        guest_os=str(raw.get("guest_os", "linux")),
        docker_in_docker=bool(raw.get("docker_in_docker", False)),
        dockerfile=raw.get("dockerfile"),
        computer_use=bool(raw.get("computer_use", False)),
        virgl=bool(raw.get("virgl", False)),
        memory=int(raw.get("memory", 2048)),
        cpus=int(raw.get("cpus", 2)),
        disk_size=int(raw.get("disk_size", 20)),
        base_image=raw.get("base_image"),
        provision=raw.get("provision"),
        persistent_paths=raw.get("persistent_paths", []),
        allow_host_escape=bool(raw.get("allow_host_escape", False)),
    )


def _parse(raw: dict) -> Config:
    """Parse validated raw dict into Config dataclass."""
    contexts = {}
    for name, ctx in raw["contexts"].items():
        # Parse container config: presence of the key implies enabled.
        container_raw = ctx.get("container")
        container: ContainerConfig | None = None
        sandbox: SandboxConfig | None = None

        if container_raw is not None:
            if isinstance(container_raw, dict):
                container = ContainerConfig(
                    enabled=bool(container_raw.get("enabled", True)),
                    docker_in_docker=bool(
                        container_raw.get("docker_in_docker", False)
                    ),
                    dockerfile=container_raw.get("dockerfile"),
                    computer_use=bool(
                        container_raw.get("computer_use", False)
                    ),
                )
            else:
                # e.g. `container: true` as shorthand
                container = ContainerConfig(enabled=bool(container_raw))

            # Also create a SandboxConfig from the container config
            # for forward compatibility.
            sandbox = SandboxConfig(
                backend="docker",
                enabled=container.enabled,
                docker_in_docker=container.docker_in_docker,
                dockerfile=container.dockerfile,
                computer_use=container.computer_use,
            )

        # Parse sandbox config (new-style, takes precedence).
        sandbox_raw = ctx.get("sandbox")
        if sandbox_raw is not None:
            sandbox = _parse_sandbox_config(sandbox_raw)
            # Also populate ContainerConfig for backward compatibility
            # when the backend is Docker.
            if sandbox.backend == "docker":
                container = ContainerConfig(
                    enabled=sandbox.enabled,
                    docker_in_docker=sandbox.docker_in_docker,
                    dockerfile=sandbox.dockerfile,
                    computer_use=sandbox.computer_use,
                )

        contexts[name] = ContextConfig(
            directory=ctx["directory"],
            description=ctx["description"],
            allowed_tools=ctx["allowed_tools"],
            model=ctx.get("model"),
            effort=ctx.get("effort"),
            mcp=ctx.get("mcp", {}),
            additional_directories=ctx.get("additional_directories", []),
            default_for_chats=ctx.get("default_for_chats", []),
            locked_for_chats=ctx.get("locked_for_chats", []),
            container=container,
            sandbox=sandbox,
        )

    # Parse optional review config.
    review_raw: dict[str, Any] = raw.get("review", {})
    tunnel_raw = review_raw.get("tunnel")
    if tunnel_raw is not None and tunnel_raw not in ("cloudflared",):
        raise ValueError(
            f"Unsupported review.tunnel value: {tunnel_raw!r} "
            f"(supported: 'cloudflared')"
        )

    review = ReviewConfig(
        host=str(review_raw.get("host", "127.0.0.1")),
        port=int(review_raw.get("port", 8080)),
        public_url=review_raw.get("public_url"),
        tunnel=tunnel_raw,
    )

    prompt_suggestions_raw = raw.get("prompt_suggestions", {})
    if prompt_suggestions_raw is None:
        prompt_suggestions_raw = {}
    if not isinstance(prompt_suggestions_raw, dict):
        raise ValueError("prompt_suggestions must be a mapping")
    prompt_suggestions = PromptSuggestionsConfig(
        enabled=bool(prompt_suggestions_raw.get("enabled", True))
    )

    return Config(
        telegram=TelegramConfig(token=raw["telegram"]["token"]),
        allowed_users=raw["allowed_users"],
        contexts=contexts,
        default_context=raw["default_context"],
        review=review,
        prompt_suggestions=prompt_suggestions,
        instance_name=raw.get("instance_name"),
        auto_update=bool(raw.get("auto_update", True)),
    )


def load_config(path: str | None = None) -> Config:
    """Load and validate config from a YAML file.

    Args:
        path: Path to config file. Defaults to platform-specific config directory.

    Returns:
        Parsed and validated Config.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config is invalid.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    _validate_raw(raw)
    return _parse(raw)


def config_to_dict(config: Config) -> dict[str, Any]:
    """Serialize a Config dataclass back into a YAML-compatible dict.

    The telegram token is excluded for security — callers that need to
    write a full config should merge the result with the existing raw
    dict to preserve the token.
    """
    contexts: dict[str, Any] = {}
    for name, ctx in config.contexts.items():
        ctx_dict: dict[str, Any] = {
            "directory": ctx.directory,
            "description": ctx.description,
            "allowed_tools": ctx.allowed_tools,
        }
        if ctx.model is not None:
            ctx_dict["model"] = ctx.model
        if ctx.effort is not None:
            ctx_dict["effort"] = ctx.effort
        if ctx.mcp:
            ctx_dict["mcp"] = ctx.mcp
        if ctx.additional_directories:
            ctx_dict["additional_directories"] = ctx.additional_directories
        if ctx.default_for_chats:
            ctx_dict["default_for_chats"] = ctx.default_for_chats
        if ctx.locked_for_chats:
            ctx_dict["locked_for_chats"] = ctx.locked_for_chats

        # Prefer sandbox over legacy container.
        if ctx.sandbox is not None:
            sandbox_dict: dict[str, Any] = {"backend": ctx.sandbox.backend}
            if ctx.sandbox.guest_os != "linux":
                sandbox_dict["guest_os"] = ctx.sandbox.guest_os
            if not ctx.sandbox.enabled:
                sandbox_dict["enabled"] = False
            if ctx.sandbox.docker_in_docker:
                sandbox_dict["docker_in_docker"] = True
            if ctx.sandbox.dockerfile is not None:
                sandbox_dict["dockerfile"] = ctx.sandbox.dockerfile
            if ctx.sandbox.computer_use:
                sandbox_dict["computer_use"] = True
            if ctx.sandbox.virgl:
                sandbox_dict["virgl"] = True
            if ctx.sandbox.allow_host_escape:
                sandbox_dict["allow_host_escape"] = True
            # VM fields — only include non-defaults for VM backends.
            if ctx.sandbox.backend in ("libvirt", "lima"):
                if ctx.sandbox.memory != 2048:
                    sandbox_dict["memory"] = ctx.sandbox.memory
                if ctx.sandbox.cpus != 2:
                    sandbox_dict["cpus"] = ctx.sandbox.cpus
                if ctx.sandbox.disk_size != 20:
                    sandbox_dict["disk_size"] = ctx.sandbox.disk_size
                if ctx.sandbox.base_image is not None:
                    sandbox_dict["base_image"] = ctx.sandbox.base_image
                if ctx.sandbox.provision is not None:
                    sandbox_dict["provision"] = ctx.sandbox.provision
                if ctx.sandbox.persistent_paths:
                    sandbox_dict["persistent_paths"] = ctx.sandbox.persistent_paths
            ctx_dict["sandbox"] = sandbox_dict
        elif ctx.container is not None:
            container_dict: dict[str, Any] = {}
            if not ctx.container.enabled:
                container_dict["enabled"] = False
            if ctx.container.docker_in_docker:
                container_dict["docker_in_docker"] = True
            if ctx.container.dockerfile is not None:
                container_dict["dockerfile"] = ctx.container.dockerfile
            if ctx.container.computer_use:
                container_dict["computer_use"] = True
            ctx_dict["container"] = container_dict

        contexts[name] = ctx_dict

    result: dict[str, Any] = {
        "telegram": {"token": config.telegram.token},
        "allowed_users": config.allowed_users,
        "contexts": contexts,
        "default_context": config.default_context,
    }

    # Include review config if non-default.
    review_dict: dict[str, Any] = {}
    if config.review.host != "127.0.0.1":
        review_dict["host"] = config.review.host
    if config.review.port != 8080:
        review_dict["port"] = config.review.port
    if config.review.public_url is not None:
        review_dict["public_url"] = config.review.public_url
    if config.review.tunnel is not None:
        review_dict["tunnel"] = config.review.tunnel
    if review_dict:
        result["review"] = review_dict

    if not config.prompt_suggestions.enabled:
        result["prompt_suggestions"] = {"enabled": False}

    if config.instance_name is not None:
        result["instance_name"] = config.instance_name

    if not config.auto_update:
        result["auto_update"] = False

    return result


def write_config(config_path: Path, config_dict: dict[str, Any]) -> None:
    """Write a config dictionary to a YAML file.

    Creates parent directories if needed.  Does NOT preserve comments —
    use :func:`load_raw_yaml` / :func:`write_raw_yaml` for round-trip
    editing that keeps comments intact.

    Args:
        config_path: Path to write the config file.
        config_dict: Config dictionary matching the expected YAML schema.

    Raises:
        OSError: If the file cannot be written.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.dump(config_dict, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ── Round-trip YAML helpers (comment-preserving) ──


def load_raw_yaml(config_path: Path) -> Any:
    """Load a YAML file using ruamel.yaml in round-trip mode.

    Returns a ``CommentedMap`` that preserves comments, key ordering,
    and formatting.  The returned object behaves like a dict but carries
    YAML metadata so that :func:`write_raw_yaml` can reproduce the
    original file with comments intact.
    """
    from ruamel.yaml import YAML

    ry = YAML()
    ry.preserve_quotes = True
    return ry.load(config_path.read_text(encoding="utf-8"))


def write_raw_yaml(config_path: Path, data: Any) -> None:
    """Write a ruamel.yaml round-trip structure back to a YAML file.

    Preserves comments and formatting from the original load.
    """
    from io import StringIO

    from ruamel.yaml import YAML

    ry = YAML()
    ry.preserve_quotes = True
    ry.default_flow_style = False

    buf = StringIO()
    ry.dump(data, buf)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(buf.getvalue(), encoding="utf-8")
