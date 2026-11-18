"""Handler wiring smoke tests.

Building an Application does not contact Telegram, so a syntactically valid
token is enough to prove that the command handlers and filters compose.
"""

from __future__ import annotations

import pytest

import kazesu_bot as bot


def make_config(allowed: tuple[int, ...] = ()) -> bot.Config:
    return bot.Config(
        token="123456:TEST-TOKEN",
        github_token=None,
        repo="KazeSU/KazeSU",
        site_repo="KazeSU/KazeSU.github.io",
        website="https://kazesu.github.io",
        telegram_url="https://t.me/KazeKSU",
        devices_url="https://example.test/repos.json",
        allowed_chat_ids=allowed,
        webhook_url=None,
        webhook_secret=None,
        port=8080,
        log_level="INFO",
    )


@pytest.mark.parametrize("allowed", [(), (-1001, -1002)])
def test_build_application_registers_handlers(allowed: tuple[int, ...]) -> None:
    app = bot.build_application(make_config(allowed))
    try:
        commands = {
            command
            for group in app.handlers.values()
            for handler in group
            for command in getattr(handler, "commands", ())
        }
        assert {"start", "help", "latest", "download", "devices"} <= commands
        assert {"status", "changelog", "issue", "id", "ping"} <= commands
        assert app.bot_data["config"].allowed_chat_ids == allowed
    finally:
        # The GitHub client owns an httpx.AsyncClient; close it without
        # running the event loop.
        import asyncio

        asyncio.run(app.bot_data["github"].aclose())
