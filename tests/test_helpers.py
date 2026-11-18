"""Unit tests for the pure helpers in kazesu_bot.

No network and no Telegram connection: only text/asset shaping is covered.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import kazesu_bot as bot


def asset(name: str, url: str = "") -> dict:
    return {
        "name": name,
        "browser_download_url": url or f"https://example.test/{name}",
    }


def test_asset_abi_matches_longest_names_first() -> None:
    assert bot.asset_abi("KazeSU_1.0_arm64-v8a-release.apk") == "arm64-v8a"
    assert bot.asset_abi("KazeSU_1.0_armeabi-v7a-release.apk") == "armeabi-v7a"
    assert bot.asset_abi("KazeSU_1.0-x86_64-release.apk") == "x86_64"
    assert bot.asset_abi("KazeSU_1.0-riscv64-release.apk") == "riscv64"
    assert bot.asset_abi("KazeSU_1.0-universal-release.apk") == "universal"
    assert bot.asset_abi("nothing-useful.txt") is None


def test_apk_assets_filters_and_sorts() -> None:
    release = {
        "assets": [
            asset("KazeSU_universal-release.apk"),
            asset("KazeSU_arm64-v8a-release.apk"),
            asset("aarch64-android14-6.1_kernelsu.ko"),
            asset("checksums.txt"),
        ]
    }
    labels = [label for label, _ in bot.apk_assets(release)]
    assert labels == ["arm64-v8a", "universal"]


def test_ko_assets_only_kernel_modules() -> None:
    release = {"assets": [asset("a.ko"), asset("b.APK"), asset("c.ko")]}
    assert [a["name"] for a in bot.ko_assets(release)] == ["a.ko", "c.ko"]


def test_release_tag_falls_back_to_name() -> None:
    assert bot.release_tag({"tag_name": "v1.0.0-beta1"}) == "v1.0.0-beta1"
    assert bot.release_tag({"name": "Latest"}) == "Latest"
    assert bot.release_tag({}) == "unknown"


def test_run_state_covers_ci_outcomes() -> None:
    assert bot.run_state(None) == ("❔", "no runs")
    assert bot.run_state({"status": "queued"}) == ("⏳", "queued")
    assert bot.run_state({"status": "in_progress"}) == ("🔄", "in progress")
    assert bot.run_state({"status": "completed", "conclusion": "success"}) == (
        "✅",
        "success",
    )
    assert bot.run_state({"status": "completed", "conclusion": "failure"}) == (
        "❌",
        "failure",
    )
    assert bot.run_state({"status": "completed", "conclusion": "cancelled"}) == (
        "⚪",
        "cancelled",
    )


def test_parse_ts_and_fmt_date_are_utc() -> None:
    parsed = bot.parse_ts("2026-11-18T05:02:20Z")
    assert parsed == datetime(2026, 11, 18, 5, 2, 20, tzinfo=timezone.utc)
    assert bot.fmt_date(parsed) == "18 Nov 2026"
    assert bot.parse_ts(None) is None
    assert bot.parse_ts("not a date") is None


def test_chunk_text_respects_limit_and_keeps_content() -> None:
    text = "\n".join(f"line {i}" for i in range(400))
    chunks = bot.chunk_text(text, limit=200)
    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "line 0" in chunks[0]
    assert "line 399" in chunks[-1]


def test_chunk_text_splits_a_single_oversized_line() -> None:
    chunks = bot.chunk_text("x" * 250, limit=100)
    assert [len(chunk) for chunk in chunks] == [100, 100, 50]


def test_chunk_text_always_returns_something() -> None:
    assert bot.chunk_text("") == [""]


def test_markdown_to_text_strips_formatting() -> None:
    source = "# Heading\n\n- **bold** and `code`\n\n[link](https://example.test)\n"
    result = bot.markdown_to_text(source)
    assert "#" not in result
    assert "**" not in result
    assert "`" not in result
    assert "link (https://example.test)" in result
    assert "Heading" in result


def test_release_text_lists_apks_and_ko_count() -> None:
    release = {
        "tag_name": "v1.0.0-beta1",
        "published_at": "2026-11-18T05:02:20Z",
        "prerelease": True,
        "assets": [
            asset("KazeSU_arm64-v8a-release.apk"),
            asset("aarch64-android14-6.1_kernelsu.ko"),
        ],
    }
    text = bot.release_text(release)
    assert "v1.0.0-beta1" in text
    assert "pre-release" in text
    assert "arm64-v8a" in text
    assert "/download ko" in text


def test_config_from_env_parses_defaults_and_allowed_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " 123:abc ")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "-1001, -1002")
    monkeypatch.setenv("WEBHOOK_URL", "kazesu-bot.onrender.com/")
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    config = bot.Config.from_env()
    assert config.token == "123:abc"
    assert config.allowed_chat_ids == (-1001, -1002)
    assert config.webhook_url == "https://kazesu-bot.onrender.com"
    assert config.repo == "KazeSU/KazeSU"
    assert config.webhook_secret
    assert config.webhook_secret_generated is True


def test_config_uses_render_external_url_for_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://kazesu-bot.onrender.com/")
    config = bot.Config.from_env()
    assert config.webhook_url == "https://kazesu-bot.onrender.com"
    assert config.port == 8080


def test_config_keeps_an_explicit_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test")
    monkeypatch.setenv("WEBHOOK_SECRET", "fixed_secret-123")
    config = bot.Config.from_env()
    assert config.webhook_secret == "fixed_secret-123"
    assert config.webhook_secret_generated is False


def test_config_rejects_an_invalid_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test")
    monkeypatch.setenv("WEBHOOK_SECRET", "not valid!")
    with pytest.raises(SystemExit):
        bot.Config.from_env()


def test_config_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        bot.Config.from_env()


def test_config_rejects_non_numeric_chat_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "@KazeKSU")
    with pytest.raises(SystemExit):
        bot.Config.from_env()
