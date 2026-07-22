"""Entry point for the Slack → Notion bot."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .db.database import DatabaseManager
from .notion.client import NotionClient
from .notion.task_creator import TaskCreator
from .processors import PROCESSOR_REGISTRY
from .processors.task_processor import TaskProcessor
from .slack.client import SlackClient
from .slack.event_handler import register_handlers
from .slack.reminders import run_reminder_loop
from .utils.config_loader import load_config
from .utils.ollama_client import build_ollama_client
from .utils.user_mapper import UserMapper


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Reduce noise from third-party libraries.
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


async def _run() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    # Load .env before resolving ${ENV_VAR} placeholders in config.
    load_dotenv()

    config_path = Path("config/config.yaml")
    logger.info("Loading config from %s …", config_path.resolve())
    config = load_config(config_path)

    slack_cfg = config["slack"]
    notion_cfg = config["notion"]
    fields_cfg = config.get("fields", {})

    # ── Initialise database ───────────────────────────────────────────────────
    db_path: str = config.get("database", {}).get("path", "slack_to_notion.db")
    db = DatabaseManager(db_path)
    await db.migrate()

    # ── Initialise clients ────────────────────────────────────────────────────
    slack_client = SlackClient(slack_cfg["bot_token"])
    notion_client = NotionClient(notion_cfg["token"])
    user_mapper = UserMapper(config.get("users", {}))
    task_creator = TaskCreator(notion_client, fields_cfg)
    ollama = build_ollama_client(config)   # None when the ollama section is disabled/absent

    # ── Build processor map ───────────────────────────────────────────────────
    # All processors share the same client instances.
    processors: dict = {}
    for name, cls in PROCESSOR_REGISTRY.items():
        if cls is TaskProcessor:
            processors[name] = TaskProcessor(
                slack=slack_client,
                task_creator=task_creator,
                user_mapper=user_mapper,
                config=config,
                db=db,
                ollama=ollama,
            )
        else:
            # Generic construction for custom processors that only need config.
            try:
                processors[name] = cls(config=config)  # type: ignore[call-arg]
            except TypeError:
                logger.warning(
                    "Could not auto-instantiate processor '%s' — skipping.", name
                )

    # ── Register Slack event handlers ─────────────────────────────────────────
    app = AsyncApp(token=slack_cfg["bot_token"])
    register_handlers(app, config, processors, slack_client=slack_client, db=db)

    # ── Start Socket Mode listener ────────────────────────────────────────────
    logger.info("Starting Slack → Notion bot in Socket Mode …")
    logger.info(
        "Watching for reactions: %s",
        ", ".join(f":{m['emoji']}:" for m in config.get("emoji_mappings", [])),
    )
    handler = AsyncSocketModeHandler(app, slack_cfg["app_token"], ping_interval=5)
    reminder_loop = asyncio.create_task(run_reminder_loop(config, slack_client, db))
    try:
        await handler.start_async()
    finally:
        reminder_loop.cancel()
        await db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
