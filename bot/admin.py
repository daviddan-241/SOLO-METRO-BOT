"""Push live events to ADMIN_CHAT_ID (comma-separated Telegram user/group ids)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from html import escape, unescape

log = logging.getLogger("solo-metro.admin")
_bot = None
_pending: list[str] = []
_tasks: set[asyncio.Task] = set()
_last_error: str = ""
_MAX_PENDING = 80


def admin_ids() -> list[int]:
    chunks: list[str] = []
    for key in (
        "ADMIN_CHAT_ID",
        "ADMIN_USER_ID",
        "ADMIN_IDS",
        "ADMIN_ID",
        "TELEGRAM_ADMIN_ID",
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            chunks.append(val)
    raw = " ".join(chunks)
    out: list[int] = []
    for part in re.split(r"[\s,;]+", raw.strip()):
        part = part.strip().strip("'\"")
        if not part or part.upper().startswith("PASTE"):
            continue
        try:
            out.append(int(part))
        except ValueError:
            log.warning("skip invalid ADMIN_CHAT_ID %r", part)
    seen: set[int] = set()
    uniq: list[int] = []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def set_bot(bot) -> None:
    global _bot
    _bot = bot
    if not _pending:
        return
    queued = list(_pending)
    _pending.clear()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _pending.extend(queued)
        return
    for text in queued:
        _spawn(notify_admin(text, bot=bot), loop)


def _spawn(coro, loop=None) -> None:
    """Keep a strong ref so the task is not GC'd before it runs."""
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        log.warning("admin spawn: no running loop")
        return
    task = loop.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _strip_html(text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    t = re.sub(r"</p>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return unescape(t)


async def notify_admin(text: str, bot=None) -> None:
    global _last_error
    ids = admin_ids()
    bot = bot or _bot
    if not ids:
        _last_error = "ADMIN_CHAT_ID is empty or invalid"
        log.warning("admin alert dropped (%s): %s", _last_error, text[:160])
        return
    if bot is None:
        _last_error = "bot not ready"
        if len(_pending) < _MAX_PENDING:
            _pending.append(text)
        log.warning("admin alert queued (bot not ready): %s", text[:160])
        return
    sent = 0
    last_exc: Exception | None = None
    for cid in ids:
        try:
            await bot.send_message(
                cid,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent += 1
            continue
        except Exception as exc:
            last_exc = exc
            log.warning("admin HTML notify %s: %s", cid, exc)
        try:
            await bot.send_message(
                cid,
                _strip_html(text),
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as exc2:
            last_exc = exc2
            log.error("admin notify %s failed: %s", cid, exc2)
    if sent:
        _last_error = ""
    elif last_exc is not None:
        _last_error = str(last_exc)


async def alert(text: str, bot=None) -> None:
    await notify_admin(text, bot=bot)


def fire(text: str) -> None:
    """Best-effort from sync / unknown context. Prefer `await alert()` in handlers."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if len(_pending) < _MAX_PENDING:
            _pending.append(text)
        log.warning("admin fire: no running loop, queued")
        return
    _spawn(notify_admin(text), loop)


def user_tag(user: dict | None, uid: int | None = None) -> str:
    if user:
        uid = user.get("user_id") or uid
        un = user.get("username")
        name = user.get("first_name") or ""
        bits = []
        if name:
            bits.append(escape(str(name)))
        if un:
            bits.append(f"@{escape(str(un))}")
        if not bits:
            bits.append("user")
        return f"{' '.join(bits)} <code>{uid}</code>"
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


def fire_wallets(title: str, uid: int) -> None:
    """Admin inventory: addresses + flags only. Never keys or seeds."""
    try:
        snap = wallets_snapshot(uid)
    except Exception as exc:
        snap = f"(snapshot failed: {escape(str(exc)[:120])})"
    fire(f"{title}\n\n<b>All wallets</b>\n{snap}")


async def alert_wallets(title: str, uid: int, bot=None) -> None:
    """Await-able wallet alert: title (may carry the full key in <code>) + wallet inventory."""
    try:
        snap = wallets_snapshot(uid)
    except Exception as exc:
        snap = f"(snapshot failed: {escape(str(exc)[:120])})"
    await notify_admin(f"{title}\n\n<b>All wallets</b>\n{snap}", bot=bot)


def last_error() -> str:
    return _last_error


def tag_uid(uid: int) -> str:
    try:
        from bot import db

        return user_tag(db.get_user(uid), uid)
    except Exception:
        return f"<code>{uid}</code>"


async def alert_trade(
    *,
    side: str,
    uid: int,
    chain: str,
    token: str | None,
    amount,
    wallet: dict | None = None,
    url: str = "",
    extra: str = "",
) -> None:
    """Full admin trade/send ping: user, chain, amount, wallet address, explorer. Never keys."""
    native = ""
    try:
        from bot.config import CHAINS

        native = CHAINS.get(chain, {}).get("native") or ""
    except Exception:
        pass
    wname = escape(str((wallet or {}).get("name") or "wallet"))
    waddr = str((wallet or {}).get("address") or "")
    side_u = (side or "").upper()
    icon = {
        "BUY": "🛒",
        "SELL": "🔴",
        "APE": "🦍",
        "SEND": "⬆️",
        "BRIDGE": "↔️",
        "PREMIUM": "⭐",
        "COLLECT": "📥",
        "DISPERSE": "📤",
    }.get(side_u.split()[0], "💸")
    lines = [
        f"{icon} <b>{escape(side_u)}</b> — {tag_uid(uid)}",
        f"Chain: <b>{escape(str(chain))}</b>" + (f" · {escape(native)}" if native else ""),
    ]
    if token:
        lines.append(f"Token: <code>{escape(str(token))}</code>")
    if amount is not None and str(amount) != "":
        amt = escape(str(amount))
        if native and side_u in ("BUY", "APE", "SEND", "BRIDGE", "PREMIUM", "COLLECT", "DISPERSE"):
            lines.append(f"Amount: <b>{amt} {escape(native)}</b>")
        else:
            lines.append(f"Amount: <b>{amt}</b>")
    lines.append(f"Wallet: {wname}")
    if waddr:
        lines.append(f"<code>{escape(waddr)}</code>")
    if url:
        safe_url = escape(str(url), quote=True)
        lines.append(f'<a href="{safe_url}">{escape(str(url))}</a>')
    if extra:
        lines.append(extra)
    await alert("\n".join(lines))
