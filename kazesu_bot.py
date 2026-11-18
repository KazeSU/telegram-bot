#!/usr/bin/env python3
"""KazeSU Telegram companion bot.

Answers /latest, /download, /devices, /docs, /status, /changelog and /issue
from the public KazeSU GitHub releases and website.

By default the bot long-polls Telegram. Set WEBHOOK_URL (and optionally
WEBHOOK_SECRET) to serve a webhook plus a /healthz endpoint instead; the
bundled Render blueprint uses that mode because it can wake a sleeping
free instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx
from aiohttp import web
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

LOGGER = logging.getLogger("kazesu-bot")

MAX_MESSAGE = 3800
USER_AGENT = "KazeSU-bot/1.0 (+https://github.com/KazeSU/KazeSU)"
API = "https://api.github.com"

ABI_ORDER = ("arm64-v8a", "armeabi-v7a", "x86_64", "riscv64", "universal")
ABI_ALIASES = {
    "arm64": "arm64-v8a",
    "arm64-v8a": "arm64-v8a",
    "aarch64": "arm64-v8a",
    "arm": "armeabi-v7a",
    "arm32": "armeabi-v7a",
    "armv7": "armeabi-v7a",
    "armeabi-v7a": "armeabi-v7a",
    "x86": "x86_64",
    "x86_64": "x86_64",
    "riscv": "riscv64",
    "riscv64": "riscv64",
    "universal": "universal",
    "all": "universal",
}

DOCS: tuple[tuple[str, str, str], ...] = (
    ("Introduction", "/guide/introduce", "overview what is kazesu root gki"),
    ("Installation", "/guide/install", "install manager apk flash boot image patch"),
    ("Build", "/guide/build", "build kernel compile toolchain gki"),
    ("FAQ", "/guide/faq", "faq questions troubleshooting bootloop"),
    (
        "Unofficial devices",
        "/guide/unofficial-devices",
        "devices kernels maintainers ports",
    ),
)

EN_COMMANDS = [
    BotCommand("start", "About KazeSU and the command list"),
    BotCommand("latest", "Newest release and download links"),
    BotCommand("download", "Manager APK links by ABI"),
    BotCommand("devices", "Unofficial kernels and supported devices"),
    BotCommand("docs", "Install, build and FAQ guides"),
    BotCommand("status", "Latest main and dev CI builds"),
    BotCommand("changelog", "Release notes"),
    BotCommand("issue", "Report a bug or request a feature"),
    BotCommand("ping", "Check that the bot is alive"),
]

ZH_COMMANDS = [
    BotCommand("start", "关于 KazeSU 和命令列表"),
    BotCommand("latest", "最新版本与下载链接"),
    BotCommand("download", "按 ABI 获取管理器 APK"),
    BotCommand("devices", "非官方内核与支持设备"),
    BotCommand("docs", "安装、构建与常见问题"),
    BotCommand("status", "main 与 dev 的最新 CI"),
    BotCommand("changelog", "更新日志"),
    BotCommand("issue", "报告问题或请求新功能"),
    BotCommand("ping", "检查机器人是否在线"),
]


@dataclass(frozen=True)
class Config:
    token: str
    github_token: str | None
    repo: str
    site_repo: str
    website: str
    telegram_url: str
    devices_url: str
    allowed_chat_ids: tuple[int, ...]
    webhook_url: str | None
    webhook_secret: str | None
    port: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")

        website = os.environ.get("KAZESU_WEBSITE", "https://kazesu.github.io").rstrip(
            "/"
        )
        repo = os.environ.get("KAZESU_REPO", "KazeSU/KazeSU").strip()
        site_repo = os.environ.get(
            "KAZESU_SITE_REPO", "KazeSU/KazeSU.github.io"
        ).strip()
        devices_url = os.environ.get(
            "KAZESU_DEVICES_URL",
            "https://raw.githubusercontent.com/KazeSU/KazeSU.github.io/main/docs/repos.json",
        ).strip()

        allowed_raw = os.environ.get("ALLOWED_CHAT_IDS", "").replace(",", " ").split()
        try:
            allowed = tuple(int(value) for value in allowed_raw)
        except ValueError as exc:
            raise SystemExit(f"ALLOWED_CHAT_IDS must be numeric: {exc}") from exc

        webhook = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
        if webhook and not webhook.startswith(("https://", "http://")):
            webhook = f"https://{webhook}"
        secret = os.environ.get("WEBHOOK_SECRET", "").strip() or None

        return cls(
            token=token,
            github_token=os.environ.get("GITHUB_TOKEN", "").strip() or None,
            repo=repo,
            site_repo=site_repo,
            website=website,
            telegram_url=os.environ.get(
                "KAZESU_TELEGRAM_URL", "https://t.me/KazeKSU"
            ).strip(),
            devices_url=devices_url,
            allowed_chat_ids=allowed,
            webhook_url=webhook or None,
            webhook_secret=secret,
            port=int(os.environ.get("PORT", "8080")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%d %b %Y")


def fmt_ago(value: datetime) -> str:
    seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def asset_url(asset: dict[str, Any]) -> str:
    return str(asset.get("browser_download_url") or asset.get("url") or "")


def asset_abi(name: str) -> str | None:
    lower = name.lower()
    for abi in ABI_ORDER:
        if abi in lower:
            return abi
    return None


def apk_assets(release: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".apk"):
            continue
        found.append((asset_abi(name) or name, asset))
    found.sort(
        key=lambda item: (
            ABI_ORDER.index(item[0]) if item[0] in ABI_ORDER else len(ABI_ORDER)
        )
    )
    return found


def ko_assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        asset
        for asset in release.get("assets") or []
        if str(asset.get("name") or "").lower().endswith(".ko")
    ]


def release_tag(release: dict[str, Any]) -> str:
    return str(release.get("tag_name") or release.get("name") or "unknown")


def release_keyboard(release: dict[str, Any]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for label, asset in apk_assets(release):
        url = asset_url(asset)
        if url:
            rows.append([InlineKeyboardButton(label, url=url)])
    page = str(release.get("html_url") or "")
    if page:
        rows.append([InlineKeyboardButton("Release page", url=page)])
    return InlineKeyboardMarkup(rows) if rows else None


def release_text(release: dict[str, Any]) -> str:
    tag = release_tag(release)
    published = parse_ts(release.get("published_at") or release.get("created_at"))
    lines = [f"<b>KazeSU Manager · {esc(tag)}</b>"]
    meta = []
    if published:
        meta.append(fmt_date(published))
    meta.append("pre-release" if release.get("prerelease") else "stable")
    lines.append(" · ".join(meta))

    apks = apk_assets(release)
    if apks:
        lines.append("")
        lines.append("<b>Manager APKs</b>")
        for label, asset in apks:
            url = asset_url(asset)
            if url:
                lines.append(f'• <a href="{attr(url)}">{esc(label)}</a>')

    kos = ko_assets(release)
    if kos:
        lines.append("")
        lines.append(
            f"Kernel modules: {len(kos)} <code>.ko</code> assets. "
            "Use <code>/download ko</code> to list them."
        )
    return "\n".join(lines)


def run_state(run: dict[str, Any] | None) -> tuple[str, str]:
    if not run:
        return "❔", "no runs"
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if status in {"queued", "requested", "waiting", "pending"}:
        return "⏳", "queued"
    if status == "in_progress":
        return "🔄", "in progress"
    if conclusion == "success":
        return "✅", "success"
    if conclusion == "failure":
        return "❌", "failure"
    if conclusion == "cancelled":
        return "⚪", "cancelled"
    if conclusion == "skipped":
        return "⏭", "skipped"
    return "❔", conclusion or status or "unknown"


def markdown_to_text(markdown: str) -> str:
    text = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks or [""]


async def reply(update: Update, text: str, **kwargs: Any) -> None:
    message = update.effective_message
    if message is None:
        return
    for index, chunk in enumerate(chunk_text(text)):
        if index == 0:
            await message.reply_text(chunk, **kwargs)
        else:
            await message.reply_text(chunk)


class GitHub:
    """Small cached GitHub/raw client shared by every handler."""

    def __init__(self, repo: str, token: str | None) -> None:
        self.repo = repo
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(timeout=20, headers=headers)
        self._cache: dict[str, tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, ttl: float = 60) -> Any:
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
        response = await self._client.get(url)
        response.raise_for_status()
        data = response.json()
        self._cache[url] = (now + ttl, data)
        return data

    async def latest_release(self) -> dict[str, Any] | None:
        releases = await self._get(
            f"{API}/repos/{self.repo}/releases?per_page=15", ttl=120
        )
        for release in releases:
            if not release.get("draft"):
                return release
        return None

    async def workflow_run(
        self, branch: str, workflow: str = "build-manager.yml"
    ) -> dict[str, Any] | None:
        data = await self._get(
            f"{API}/repos/{self.repo}/actions/workflows/{workflow}/runs"
            f"?branch={branch}&per_page=1",
            ttl=45,
        )
        runs = data.get("workflow_runs") or []
        return runs[0] if runs else None

    async def devices(self, url: str) -> list[dict[str, Any]]:
        data = await self._get(url, ttl=600)
        return data if isinstance(data, list) else []


def github_error(exc: Exception) -> str:
    return (
        "GitHub is not answering right now "
        f"(<code>{esc(exc.__class__.__name__)}</code>). Try again in a minute."
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    text = (
        "<b>KazeSU</b>\n"
        "A kernel-based root solution for GKI devices.\n\n"
        "<b>Commands</b>\n"
        "/latest — newest release and download links\n"
        "/download — manager APK links by ABI\n"
        "/devices — unofficial kernels and supported devices\n"
        "/docs — install, build and FAQ guides\n"
        "/status — latest main and dev CI builds\n"
        "/changelog — release notes\n"
        "/issue — bug report and device request links\n"
        "/ping — check that the bot is alive\n\n"
        "This bot links to official assets; builds in the Telegram dev "
        "topic are for testing only."
    )
    rows = [
        [
            InlineKeyboardButton("Website", url=config.website),
            InlineKeyboardButton("GitHub", url=f"https://github.com/{config.repo}"),
        ],
        [
            InlineKeyboardButton("Telegram", url=config.telegram_url),
            InlineKeyboardButton(
                "Issues",
                url=f"https://github.com/{config.repo}/issues",
            ),
        ],
    ]
    await reply(update, text, reply_markup=InlineKeyboardMarkup(rows))


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    github: GitHub = context.application.bot_data["github"]
    channel = (context.args[0].lower() if context.args else "").strip()
    try:
        if channel in {"dev", "development", "nightly"}:
            run = await github.workflow_run("dev")
            if not run:
                await reply(update, "No dev builds have run yet.")
                return
            icon, label = run_state(run)
            created = parse_ts(run.get("created_at"))
            lines = ["<b>Latest dev build</b>"]
            sha = str(run.get("head_sha") or "")[:7]
            meta = [f"<code>{esc(sha)}</code>", f"{icon} {esc(label)}"]
            if created:
                meta.append(fmt_ago(created))
            lines.append(" · ".join(meta))
            if run.get("html_url"):
                lines.append(
                    f'\n<a href="{attr(run["html_url"])}">Open the workflow run</a>'
                )
            lines.append(
                "\nDev APKs are posted in the Telegram dev topic. GitHub Actions "
                "artifacts require a signed-in GitHub account."
            )
            await reply(update, "\n".join(lines))
            return

        release = await github.latest_release()
        if not release:
            await reply(update, "No releases found yet.")
            return
        await reply(
            update, release_text(release), reply_markup=release_keyboard(release)
        )
    except httpx.HTTPError as exc:
        await reply(update, github_error(exc))


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    github: GitHub = context.application.bot_data["github"]
    target = context.args[0].lower().strip() if context.args else ""
    try:
        release = await github.latest_release()
    except httpx.HTTPError as exc:
        await reply(update, github_error(exc))
        return
    if not release:
        await reply(update, "No releases found yet.")
        return

    tag = release_tag(release)
    apks = dict(apk_assets(release))

    if target in {"ko", "kernel", "lkm"}:
        modules = ko_assets(release)
        if not modules:
            await reply(update, f"{esc(tag)} has no kernel modules attached.")
            return
        lines = [
            f"<b>Kernel modules · {esc(tag)}</b>",
            "Pick the module that matches your Android release and kernel version.",
            "",
        ]
        for asset in modules[:30]:
            name = str(asset.get("name") or "")
            url = asset_url(asset)
            if url:
                lines.append(f'• <a href="{attr(url)}">{esc(name)}</a>')
        if len(modules) > 30:
            lines.append(f"\n…and {len(modules) - 30} more in the release.")
        keyboard = None
        if release.get("html_url"):
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Release page", url=str(release["html_url"]))]]
            )
        await reply(update, "\n".join(lines), reply_markup=keyboard)
        return

    if not target:
        lines = [
            f"<b>Download KazeSU Manager · {esc(tag)}</b>",
            "Pick the ABI that matches your device:",
            "• <code>arm64-v8a</code> — most modern phones",
            "• <code>armeabi-v7a</code> — older 32-bit devices",
            "• <code>x86_64</code> — emulators and x86 tablets",
            "• <code>riscv64</code> — RISC-V devices",
            "• <code>universal</code> — works everywhere, larger",
            "",
            "Kernel modules: <code>/download ko</code>",
        ]
        rows = [
            [InlineKeyboardButton(label, url=asset_url(asset))]
            for label, asset in apk_assets(release)
            if asset_url(asset)
        ]
        if release.get("html_url"):
            rows.append(
                [InlineKeyboardButton("Release page", url=str(release["html_url"]))]
            )
        await reply(
            update,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
        return

    abi = ABI_ALIASES.get(target)
    if abi is None:
        await reply(
            update,
            "Unknown ABI. Use <code>arm64-v8a</code>, <code>armeabi-v7a</code>, "
            "<code>x86_64</code>, <code>riscv64</code>, <code>universal</code> "
            "or <code>ko</code>.",
        )
        return
    asset = apks.get(abi)
    if asset is None:
        await reply(update, f"{esc(tag)} has no <code>{esc(abi)}</code> APK.")
        return
    url = asset_url(asset)
    rows = [[InlineKeyboardButton("Download", url=url)]] if url else []
    if release.get("html_url"):
        rows.append(
            [InlineKeyboardButton("Release page", url=str(release["html_url"]))]
        )
    await reply(
        update,
        f"<b>KazeSU Manager · {esc(tag)}</b>\n"
        f"ABI: <code>{esc(abi)}</code>\n"
        f'<a href="{attr(url)}">{esc(str(asset.get("name") or abi))}</a>',
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    github: GitHub = context.application.bot_data["github"]
    query = " ".join(context.args).strip().lower()
    try:
        entries = await github.devices(config.devices_url)
    except httpx.HTTPError as exc:
        await reply(update, github_error(exc))
        return

    if query:
        entries = [
            entry
            for entry in entries
            if query
            in " ".join(
                str(entry.get(key) or "")
                for key in ("maintainer", "kernel_name", "devices", "kernel_link")
            ).lower()
        ]
    if not entries:
        await reply(
            update,
            "No matching device entries. "
            "Anyone can add a kernel to the list with an Add Device issue.",
        )
        return

    shown = entries[:20]
    lines = [
        f"<b>Unofficial KazeSU devices</b> · {len(entries)} match(es)",
        "",
    ]
    for entry in shown:
        name = esc(entry.get("kernel_name") or "unknown kernel")
        maintainer = esc(entry.get("maintainer") or "unknown maintainer")
        devices = esc(entry.get("devices") or "")
        link = str(entry.get("kernel_link") or "")
        lines.append(f"• <b>{name}</b> — {maintainer}")
        if devices:
            lines.append(f"  {devices}")
        if link:
            lines.append(f'  <a href="{attr(link)}">Repository</a>')
        lines.append("")
    if len(entries) > len(shown):
        lines.append(
            f"Showing {len(shown)} of {len(entries)}. Refine with "
            "<code>/devices &lt;device or maintainer&gt;</code>."
        )

    website = f"{config.website}/guide/unofficial-devices"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Device page", url=website)],
            [
                InlineKeyboardButton(
                    "Add device",
                    url=f"https://github.com/{config.site_repo}/issues/new"
                    "?template=add_device.yml",
                )
            ],
        ]
    )
    await reply(update, "\n".join(lines), reply_markup=keyboard)


async def cmd_docs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    query = " ".join(context.args).strip().lower()
    pages = DOCS
    if query:
        pages = tuple(
            page
            for page in DOCS
            if query in page[0].lower() or query in page[2].lower()
        )
    if not pages:
        await reply(
            update,
            "No matching guide. Try <code>/docs install</code>, "
            "<code>/docs build</code> or <code>/docs faq</code>.",
        )
        return

    lines = ["<b>KazeSU documentation</b>", ""]
    for title, path, _keywords in pages:
        lines.append(f'• <a href="{attr(config.website + path)}">{esc(title)}</a>')
    lines.append("")
    lines.append(f'<a href="{attr(config.website)}">{esc(config.website)}</a>')
    await reply(
        update,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Website", url=config.website),
                    InlineKeyboardButton(
                        "GitHub", url=f"https://github.com/{config.repo}"
                    ),
                ]
            ]
        ),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    github: GitHub = context.application.bot_data["github"]
    release, main_run, dev_run = await asyncio.gather(
        github.latest_release(),
        github.workflow_run("main"),
        github.workflow_run("dev"),
        return_exceptions=True,
    )

    lines = ["<b>KazeSU status</b>"]

    if isinstance(release, Exception):
        lines.append("<b>Latest release</b>: unavailable")
    elif not release:
        lines.append("<b>Latest release</b>: none yet")
    else:
        tag = esc(release_tag(release))
        url = str(release.get("html_url") or "")
        published = parse_ts(release.get("published_at"))
        meta = []
        if published:
            meta.append(fmt_date(published))
        meta.append("pre-release" if release.get("prerelease") else "stable")
        label = f'<a href="{attr(url)}">{tag}</a>' if url else tag
        lines.append(f"<b>Latest release</b>: {label} · {' · '.join(meta)}")

    for branch, run in (("main", main_run), ("dev", dev_run)):
        if isinstance(run, Exception):
            lines.append(f"<b>{branch}</b>: unavailable")
            continue
        icon, label = run_state(run)
        meta = [f"{icon} {esc(label)}"]
        created = parse_ts(run.get("created_at")) if run else None
        if created:
            meta.append(fmt_ago(created))
        url = str(run.get("html_url") or "") if run else ""
        if url:
            meta.append(f'<a href="{attr(url)}">run</a>')
        lines.append(f"<b>{branch}</b>: {' · '.join(meta)}")

    await reply(update, "\n".join(lines))


async def cmd_changelog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    github: GitHub = context.application.bot_data["github"]
    try:
        release = await github.latest_release()
    except httpx.HTTPError as exc:
        await reply(update, github_error(exc))
        return
    if not release:
        await reply(update, "No releases found yet.")
        return

    tag = release_tag(release)
    body = markdown_to_text(str(release.get("body") or ""))
    if not body:
        body = "No release notes were attached to this release."
    url = str(release.get("html_url") or "")
    footer = f'\n\n<a href="{attr(url)}">Full release notes</a>' if url else ""
    header = f"<b>Changelog · {esc(tag)}</b>\n\n"
    payload = header + esc(body)
    limit = MAX_MESSAGE - len(footer)
    if len(payload) > limit:
        payload = payload[:limit].rsplit("\n", 1)[0].rstrip() + "\n…"
    await reply(update, payload + footer)


async def cmd_issue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    rows = [
        [
            InlineKeyboardButton(
                "Bug report",
                url=f"https://github.com/{config.repo}/issues/new?template=bug_report.yml",
            )
        ],
        [
            InlineKeyboardButton(
                "Feature request",
                url=f"https://github.com/{config.repo}/issues/new"
                "?template=feature_request.yml",
            )
        ],
        [
            InlineKeyboardButton(
                "Add unofficial device",
                url=f"https://github.com/{config.site_repo}/issues/new"
                "?template=add_device.yml",
            )
        ],
    ]
    await reply(
        update,
        "<b>Need help?</b>\n"
        "Check <code>/docs</code> and <code>/devices</code> first. When you open "
        "an issue, include your device, Android version, kernel version, manager "
        "build and a log if possible.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None:
        return
    lines = [
        f"Chat ID: <code>{chat.id}</code>",
        f"Chat type: <code>{esc(chat.type)}</code>",
    ]
    if message is not None and message.message_thread_id is not None:
        lines.append(f"Topic ID: <code>{message.message_thread_id}</code>")
    if update.effective_user is not None:
        lines.append(f"User ID: <code>{update.effective_user.id}</code>")
    await reply(update, "\n".join(lines))


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = time.perf_counter()
    message = update.effective_message
    if message is None:
        return
    sent = await message.reply_text("Pong!")
    elapsed = (time.perf_counter() - started) * 1000
    with contextlib.suppress(TelegramError):
        await sent.edit_text(f"Pong! <code>{elapsed:.0f} ms</code>")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text("Unknown command. Try /help.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Update handling failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text(
                "Something went wrong. Please try again in a moment."
            )


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(EN_COMMANDS)
        await app.bot.set_my_commands(ZH_COMMANDS, language_code="zh-hans")
    except TelegramError as exc:
        LOGGER.warning("Could not register the command menu: %s", exc)


def build_application(config: Config) -> Application:
    github = GitHub(config.repo, config.github_token)
    app = (
        ApplicationBuilder()
        .token(config.token)
        .defaults(
            Defaults(
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        .post_init(post_init)
        .build()
    )
    app.bot_data.update({"config": config, "github": github, "website": config.website})

    chat_filter: filters.Chat | None = (
        filters.Chat(chat_id=list(config.allowed_chat_ids))
        if config.allowed_chat_ids
        else None
    )

    def add(commands: Sequence[str], callback: Any) -> None:
        app.add_handler(CommandHandler(commands, callback, filters=chat_filter))

    add(["start", "help"], cmd_start)
    add(["latest"], cmd_latest)
    add(["download", "apk"], cmd_download)
    add(["devices", "device"], cmd_devices)
    add(["docs", "guide"], cmd_docs)
    add(["status"], cmd_status)
    add(["changelog", "changes"], cmd_changelog)
    add(["issue", "report"], cmd_issue)
    add(["id"], cmd_id)
    add(["ping"], cmd_ping)

    command_filter = filters.COMMAND
    if chat_filter is not None:
        command_filter = command_filter & chat_filter
    app.add_handler(MessageHandler(command_filter, unknown_command), group=1)
    app.add_error_handler(on_error)
    return app


async def wait_for_signal() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run_polling(app: Application, config: Config) -> None:
    if app.updater is None:
        raise RuntimeError("This application was built without an updater")
    await app.bot.delete_webhook(drop_pending_updates=False)
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=False
    )
    try:
        await app.start()
        LOGGER.info("Polling for Telegram updates")
        await wait_for_signal()
    finally:
        await app.updater.stop()


async def run_webhook(app: Application, config: Config) -> None:
    if not config.webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set")
    secret = config.webhook_secret
    if secret is None:
        LOGGER.warning(
            "WEBHOOK_SECRET is not set; anyone who guesses the webhook URL "
            "could inject fake updates"
        )
    path = "/telegram"
    url = f"{config.webhook_url}{path}"

    await app.bot.set_webhook(
        url=url,
        secret_token=secret,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    await app.start()

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def webhook(request: web.Request) -> web.Response:
        if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return web.Response(status=403)
        try:
            payload = await request.json()
            update = Update.de_json(payload, app.bot)
        except (ValueError, TypeError, KeyError):
            return web.Response(status=400)
        await app.update_queue.put(update)
        return web.Response()

    server = web.Application()
    server.router.add_get("/healthz", health)
    server.router.add_post(path, webhook)

    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    LOGGER.info("Webhook listening on %s (health: /healthz)", url)
    try:
        await wait_for_signal()
    finally:
        await runner.cleanup()


async def run(config: Config) -> None:
    app = build_application(config)
    github: GitHub = app.bot_data["github"]
    await app.initialize()
    try:
        if app.post_init is not None:
            await app.post_init(app)
        if config.webhook_url:
            await run_webhook(app, config)
        else:
            await run_polling(app, config)
    finally:
        if app.running:
            await app.stop()
        await app.shutdown()
        await github.aclose()


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    LOGGER.info(
        "Starting KazeSU bot for %s (%s)",
        config.repo,
        "webhook" if config.webhook_url else "polling",
    )
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
