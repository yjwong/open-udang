"""Spawn and manage stdio MCP server subprocesses.

Each MCP server is a child process that speaks JSON-RPC 2.0 over
stdin/stdout.  The manager keeps processes alive, serialises
request/response pairs via a per-process lock, and respawns on crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from open_shrimp.mcp_proxy.config_reader import StdioServerConfig

logger = logging.getLogger(__name__)

# 16 MiB — generous limit for MCP responses (e.g. large database query
# results).  Python's asyncio StreamReader defaults to 64 KiB which is
# far too small. Bun's default is higher, so this preserves comparable headroom.
_STDOUT_BUFFER_LIMIT = 16 * 1024 * 1024


@dataclass
class StdioProcess:
    """A running MCP stdio server process."""

    process: asyncio.subprocess.Process
    config: StdioServerConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return self.process.returncode is None


class StdioManager:
    """Manages the lifecycle of stdio MCP server processes.

    Processes are keyed by ``(context_name, server_name)`` so that
    multiple sandbox sessions sharing the same context reuse the same
    server instances.
    """

    def __init__(self) -> None:
        self._processes: dict[tuple[str, str], StdioProcess] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_spawn(
        self,
        context_name: str,
        server_name: str,
        config: StdioServerConfig,
    ) -> StdioProcess:
        """Return an existing process or spawn a new one."""
        key = (context_name, server_name)
        proc = self._processes.get(key)
        if proc is not None and proc.alive:
            return proc
        # Dead or missing — (re)spawn.
        if proc is not None:
            logger.warning(
                "MCP server '%s' for context '%s' exited (rc=%s), respawning",
                server_name,
                context_name,
                proc.process.returncode,
            )
            await self._cleanup_process(proc)
        new_proc = await self._spawn(context_name, server_name, config)
        self._processes[key] = new_proc
        return new_proc

    async def send_message(
        self,
        proc: StdioProcess,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Send a JSON-RPC message and return the response.

        For notifications (no ``id`` field) returns ``None``.
        The caller must hold no lock — this method acquires
        ``proc.lock`` internally.
        """
        is_notification = "id" not in message
        async with proc.lock:
            if not proc.alive:
                raise RuntimeError("MCP server process is not running")
            assert proc.process.stdin is not None
            assert proc.process.stdout is not None

            payload = json.dumps(message, separators=(",", ":")) + "\n"
            proc.process.stdin.write(payload.encode())
            await proc.process.stdin.drain()

            if is_notification:
                return None

            # Read lines until we get a response matching our request id.
            # Cap iterations to avoid infinite loops if the server
            # produces endless non-matching output.
            request_id = message.get("id")
            max_lines = 500
            for _ in range(max_lines):
                try:
                    line = await asyncio.wait_for(
                        proc.process.stdout.readline(),
                        timeout=120,  # generous timeout for slow tools
                    )
                except ValueError:
                    # readline() re-raises LimitOverrunError as ValueError
                    # when a single line exceeds the StreamReader buffer
                    # limit.  The stream is irrecoverably corrupted —
                    # kill the process so it gets respawned on the next
                    # request.
                    logger.error(
                        "MCP server produced a line exceeding buffer "
                        "limit (%d bytes), killing process for respawn",
                        _STDOUT_BUFFER_LIMIT,
                    )
                    proc.process.kill()
                    raise
                if not line:
                    raise RuntimeError(
                        "MCP server process closed stdout unexpectedly"
                    )
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "Non-JSON line from MCP server: %s", line[:200]
                    )
                    continue
                if "id" in response and response["id"] == request_id:
                    return response  # type: ignore[no-any-return]
                logger.debug(
                    "Skipping non-matching message from MCP server: %s",
                    json.dumps(response)[:200],
                )
            raise RuntimeError(
                f"MCP server produced {max_lines} lines without a "
                f"matching response for request id={request_id}"
            )

    async def stop_context(self, context_name: str) -> None:
        """Stop all MCP servers for *context_name*."""
        keys = [k for k in self._processes if k[0] == context_name]
        for key in keys:
            proc = self._processes.pop(key, None)
            if proc is not None:
                await self._cleanup_process(proc)

    async def stop_all(self) -> None:
        """Stop all managed MCP server processes."""
        for proc in self._processes.values():
            await self._cleanup_process(proc)
        self._processes.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _spawn(
        self,
        context_name: str,
        server_name: str,
        config: StdioServerConfig,
    ) -> StdioProcess:
        """Spawn a new stdio MCP server process."""
        # Build environment: host env + server-specific overrides.
        env = {**os.environ, **config.env}

        logger.info(
            "Spawning MCP server '%s' for context '%s': %s %s",
            server_name,
            context_name,
            config.command,
            " ".join(config.args),
        )

        process = await asyncio.create_subprocess_exec(
            config.command,
            *config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STDOUT_BUFFER_LIMIT,
        )

        proc = StdioProcess(process=process, config=config)

        # Drain stderr in the background to prevent pipe buffer deadlock.
        proc._stderr_task = asyncio.create_task(
            self._drain_stderr(context_name, server_name, process),
            name=f"mcp-stderr-{context_name}-{server_name}",
        )

        return proc

    @staticmethod
    async def _drain_stderr(
        context_name: str,
        server_name: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Read and log stderr from a server process until it exits."""
        assert process.stderr is not None
        try:
            async for line in process.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    logger.debug(
                        "[mcp:%s/%s:stderr] %s",
                        context_name,
                        server_name,
                        text,
                    )
        except Exception:
            pass  # process exited

    @staticmethod
    async def _cleanup_process(proc: StdioProcess) -> None:
        """Terminate a process and cancel its stderr drain task."""
        if proc._stderr_task is not None:
            proc._stderr_task.cancel()
            try:
                await proc._stderr_task
            except asyncio.CancelledError:
                pass
        if proc.alive:
            try:
                proc.process.terminate()
                await asyncio.wait_for(proc.process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.process.kill()
                except ProcessLookupError:
                    pass
