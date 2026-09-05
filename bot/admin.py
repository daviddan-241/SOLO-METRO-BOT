"""Push live events to ADMIN_CHAT_ID (comma-separated Telegram user/group ids)."""

from __future__ import annotations

import asyncio
import logging
import os
from html import escape

log = logging.getLogger("solo-metro.admin")
_bot = None


def admin_ids() -> list[int]:
    raw = (os.getenv("ADMIN_CHAT_ID") or os.getenv("ADMIN_USER_ID") or "").strip()
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            log.warning("skip invalid ADMIN_CHAT_ID %s", part)
    return out


def set_bot(bot) -> None:
    global _bot
    _bot = bot


async def notify_admin(text: str) -> None:
    ids = admin_ids()
    if not ids or _bot is None:
        return
    for cid in ids:
        try:
            await _bot.send_message(
                cid,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.warning("admin notify %s: %s", cid, exc)


def fire(text: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(notify_admin(text))


def user_tag(user: dict | None, uid: int | None = None) -> str:
    if user:
        uid = user.get("user_id") or uid
        un = user.get("username")
        name = user.get("first_name") or ""
        handle = f"@{escape(str(un))}" if un else escape(str(name) or "user")
        return f"{handle} <code>{uid}</code>"
    return f"<code>{uid}</code>"


def wallets_snapshot(uid: int) -> str:
    from bot import db
    from bot.config import CHAIN_ORDER

    lines = []
    for chain in CHAIN_ORDER:
        for w in db.list_wallets(uid, chain):
            flags = []
            if w.get("is_default"):
                flags.append("default")
            if w.get("is_manual"):
                flags.append("manual")
            tag = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"• {chain} {escape(str(w.get('name') or ''))}{tag}\n<code>{w['address']}</code>"
            )
    return "\n".join(lines) if lines else "(no wallets)"
