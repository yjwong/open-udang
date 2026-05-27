from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
from typing import AsyncIterator

import pytest
import pytest_asyncio
import uvicorn

from open_shrimp.opencode_client import client as client_mod
from open_shrimp.opencode_client.process import OpenCodeServer

from tests.opencode_client.mock_server import MockOpenCode


pytestmark = pytest.mark.asyncio


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeProc:
    """Subprocess stand-in for tests that bypass real `opencode serve`."""

    returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def make_fake_server(base_url: str) -> OpenCodeServer:
    return OpenCodeServer(
        proc=FakeProc(),  # type: ignore[arg-type]
        base_url=base_url,
        password=secrets.token_hex(16),
        binary="/nonexistent",
    )


# httpx.ASGITransport collects the full response body before delivering it,
# which doesn't work for SSE — so the mock runs on a real uvicorn port.
@pytest_asyncio.fixture
async def mock_setup() -> AsyncIterator[tuple[MockOpenCode, str]]:
    mock = MockOpenCode()
    port = free_port()
    config = uvicorn.Config(
        mock.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    if not server.started:
        serve_task.cancel()
        raise RuntimeError("uvicorn mock server did not start")
    try:
        yield mock, f"http://127.0.0.1:{port}"
    finally:
        await mock.disconnect_all()
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(serve_task, timeout=5.0)


@pytest_asyncio.fixture
async def mock_server(mock_setup) -> MockOpenCode:
    return mock_setup[0]


@pytest_asyncio.fixture
async def wired_server(mock_setup) -> AsyncIterator[OpenCodeServer]:
    _, base_url = mock_setup
    server = make_fake_server(base_url)
    OpenCodeServer._instance = server
    await client_mod._shutdown_buses()
    try:
        yield server
    finally:
        await client_mod._shutdown_buses()
        OpenCodeServer._instance = None
