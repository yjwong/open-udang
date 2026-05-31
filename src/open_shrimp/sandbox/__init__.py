"""Sandbox package -- isolated execution environments for OpenCode."""

from open_shrimp.sandbox.base import Sandbox
from open_shrimp.sandbox.manager import (
    DockerSandboxManager,
    LibvirtSandboxManager,
    LimaSandboxManager,
    SandboxManager,
    create_sandbox_managers,
)

__all__ = [
    "DockerSandboxManager",
    "LibvirtSandboxManager",
    "LimaSandboxManager",
    "Sandbox",
    "SandboxManager",
    "create_sandbox_managers",
]
