"""Entry point for OpenShrimp Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from open_shrimp.bot import run_bot
from open_shrimp.config import DEFAULT_CONFIG_PATH, is_sandboxed, load_config
from open_shrimp.db import init_db
from open_shrimp.paths import init_paths
from open_shrimp.sandbox import SandboxManager, create_sandbox_managers

logger = logging.getLogger("open_shrimp")

_restart_requested = False


def _dump_debug_info() -> None:
    """Dump asyncio tasks and thread stacks to stderr on SIGUSR1."""
    import faulthandler

    logger.warning("=== SIGUSR1 received — dumping debug info ===")

    # Dump all thread stacks via faulthandler (writes to stderr)
    logger.warning("--- Thread stacks ---")
    faulthandler.dump_traceback(file=sys.stderr)

    # Dump all asyncio tasks
    try:
        loop = asyncio.get_running_loop()
        tasks = asyncio.all_tasks(loop)
        logger.warning("--- Asyncio tasks (%d) ---", len(tasks))
        for task in sorted(tasks, key=lambda t: t.get_name()):
            coro = task.get_coro()
            logger.warning(
                "  Task %s: state=%s coro=%s",
                task.get_name(),
                task._state,
                coro,
            )
            # Print the task's stack frames if available
            frames = task.get_stack()
            for frame in frames:
                logger.warning(
                    "    File %s:%d in %s",
                    frame.f_code.co_filename,
                    frame.f_lineno,
                    frame.f_code.co_name,
                )
    except RuntimeError:
        logger.warning("No running event loop — skipping asyncio task dump")

    logger.warning("=== End debug dump ===")


def request_restart() -> None:
    """Signal that the process should re-exec after shutdown."""
    global _restart_requested
    _restart_requested = True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenShrimp - Telegram bot for remote Claude access")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    sub_install = subparsers.add_parser(
        "install",
        help="Install OpenShrimp as a system service (systemd/launchd)",
    )
    sub_install.add_argument(
        "--config",
        dest="config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers.add_parser(
        "uninstall",
        help="Remove the OpenShrimp system service",
    )

    subparsers.add_parser(
        "doctor",
        help="Check optional component availability",
    )

    subparsers.add_parser(
        "update",
        help="Check for and apply updates",
    )

    return parser.parse_args()


def _create_http_server(
    config: "Config",  # noqa: F821
    db: "aiosqlite.Connection",  # noqa: F821
    sandbox_managers: dict[str, SandboxManager] | None = None,
    config_path: str | None = None,
) -> "uvicorn.Server":  # noqa: F821
    """Create the review API HTTP server (call ``server.serve()`` to run)."""
    import uvicorn

    from open_shrimp.review.api import create_review_app

    app = create_review_app(
        config, db, sandbox_managers=sandbox_managers, config_path=config_path
    )

    server_config = uvicorn.Config(
        app,
        host=config.review.host,
        port=config.review.port,
        log_level="info",
    )
    server = uvicorn.Server(server_config)
    logger.info(
        "Starting review API server on %s:%d",
        config.review.host,
        config.review.port,
    )
    return server


async def run_bot_async(config_path: str, stop_event: asyncio.Event | None = None) -> None:
    """Run the bot and HTTP server until *stop_event* is set.

    This is the shared async entry point used by both the CLI (``main()``)
    and the macOS menu-bar app.  When *stop_event* is ``None`` (the CLI
    path), SIGTERM/SIGINT handlers are installed automatically.
    """
    config = load_config(config_path)
    logger.info("Config loaded from %s", config_path)
    logger.info("Contexts: %s", ", ".join(config.contexts.keys()))

    init_paths(config.instance_name)

    db = await init_db()

    # Start tunnel if configured (before the bot, so public_url is ready).
    tunnel_proc = None
    if config.review.tunnel == "cloudflared" and not config.review.public_url:
        from open_shrimp.tunnel import start_tunnel

        try:
            tunnel_proc, tunnel_url = await start_tunnel(config.review.port)
            config.review.public_url = tunnel_url
            logger.info("Tunnel URL set as public_url: %s", tunnel_url)
        except RuntimeError as e:
            logger.error("Failed to start tunnel: %s", e)
            logger.error(
                "The review app will not be accessible externally. "
                "Set review.public_url manually or fix the tunnel issue."
            )

    # Set up graceful shutdown
    if stop_event is None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
        loop.add_signal_handler(signal.SIGUSR1, _dump_debug_info)

    sandbox_mgrs = create_sandbox_managers(config)

    # Start the MCP proxy if any context uses a sandbox.  The proxy
    # runs on a separate listener so that sandboxes cannot reach the
    # main Starlette server (review-app, config-app, etc.).
    mcp_proxy = None
    if any(is_sandboxed(ctx) for ctx in config.contexts.values()):
        from open_shrimp.mcp_proxy import McpProxy

        mcp_proxy = McpProxy()
        await mcp_proxy.start()

    http_server = _create_http_server(
        config, db, sandbox_managers=sandbox_mgrs, config_path=config_path
    )

    bot_task = asyncio.create_task(
        run_bot(
            config, db,
            config_path=config_path,
            sandbox_managers=sandbox_mgrs,
            mcp_proxy=mcp_proxy,
        )
    )
    http_task = asyncio.create_task(http_server.serve())

    await stop_event.wait()
    logger.info("Shutting down...")

    # Signal uvicorn to exit gracefully (avoids CancelledError in lifespan).
    http_server.should_exit = True

    bot_task.cancel()
    for task in (bot_task, http_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Stop the MCP proxy and its stdio server processes.
    if mcp_proxy is not None:
        await mcp_proxy.shutdown()

    # Stop the tunnel if we started one.
    if tunnel_proc is not None:
        from open_shrimp.tunnel import stop_tunnel

        await stop_tunnel(tunnel_proc)

    await db.close()
    logger.info("Shutdown complete")


async def _async_main(config_path: str) -> None:
    await run_bot_async(config_path)


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    args = _parse_args()

    # Handle install/uninstall subcommands
    if args.subcommand == "install":
        from open_shrimp.service import install_service

        install_service(args.config)
        return

    if args.subcommand == "uninstall":
        from open_shrimp.service import uninstall_service

        uninstall_service()
        return

    if args.subcommand == "doctor":
        from open_shrimp.doctor import run_doctor

        sys.exit(run_doctor())

    if args.subcommand == "update":
        from open_shrimp.updater import run_update_cli

        sys.exit(asyncio.run(run_update_cli()))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY not set — will use Claude Code OAuth if available"
        )

    # Offer guided setup when config is missing and running interactively.
    config_path = Path(args.config)
    if not config_path.exists():
        if sys.stdin.isatty():
            from open_shrimp.setup import run_setup_wizard

            try:
                run_setup_wizard(config_path)
            except SystemExit:
                return
            # Config file now exists; fall through to normal startup.
        else:
            logger.error(
                "Config file not found: %s — "
                "run interactively to use the setup wizard, "
                "or copy config.example.yaml and edit it manually.",
                config_path,
            )
            sys.exit(1)

    try:
        asyncio.run(_async_main(args.config))
    except KeyboardInterrupt:
        pass

    if _restart_requested:
        logger.info("Re-executing process for restart...")
        import shutil

        from open_shrimp.updater import pyapp_binary_path

        pyapp = pyapp_binary_path()
        if pyapp:
            os.execv(str(pyapp), [str(pyapp)] + sys.argv[1:])
        else:
            uv = shutil.which("uv")
            if uv:
                # Re-exec via uv run so the venv is rebuilt if needed,
                # matching the systemd ExecStart invocation.
                os.execv(uv, [uv, "run", "openshrimp"] + sys.argv[1:])
            else:
                os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
