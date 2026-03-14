"""Entry point for OpenUdang Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from open_udang.bot import run_bot
from open_udang.config import DEFAULT_CONFIG_PATH, load_config
from open_udang.db import init_db

logger = logging.getLogger("open_udang")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenUdang - Telegram bot for remote Claude access")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


async def _run_http_server(config: "Config", db: "aiosqlite.Connection") -> None:  # noqa: F821
    """Run the review API HTTP server."""
    import uvicorn

    from open_udang.review.api import create_review_app

    app = create_review_app(config, db)

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
    await server.serve()


async def _async_main(config_path: str) -> None:
    config = load_config(config_path)
    logger.info("Config loaded from %s", config_path)
    logger.info("Contexts: %s", ", ".join(config.contexts.keys()))

    db = await init_db()

    # Start tunnel if configured (before the bot, so public_url is ready).
    tunnel_proc = None
    if config.review.tunnel == "cloudflared" and not config.review.public_url:
        from open_udang.tunnel import start_tunnel

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
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    bot_task = asyncio.create_task(run_bot(config, db))
    http_task = asyncio.create_task(_run_http_server(config, db))

    await stop_event.wait()
    logger.info("Shutting down...")

    bot_task.cancel()
    http_task.cancel()
    for task in (bot_task, http_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Stop the tunnel if we started one.
    if tunnel_proc is not None:
        from open_udang.tunnel import stop_tunnel

        await stop_tunnel(tunnel_proc)

    await db.close()
    logger.info("Shutdown complete")


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    args = _parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY not set — will use Claude Code OAuth if available"
        )

    # Offer guided setup when config is missing and running interactively.
    config_path = Path(args.config)
    if not config_path.exists():
        if sys.stdin.isatty():
            from open_udang.setup import run_setup_wizard

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


if __name__ == "__main__":
    main()
