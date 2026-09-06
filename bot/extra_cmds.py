"""Maestro-parity command aliases and extra panels (godmode, DCA, campaigns, …)."""

from __future__ import annotations

import html
import time

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import db
from bot import keyboards as kb
from bot.config import BOT_NAME, CHAINS, DOCS_URL, SUPPORT_URL
from bot.engine import approve_token, wallet_overview

BOOT_TS = time.time()


async def _h():
    from bot import handlers as h

    return h


async def _auth(update: Update):
    h = await _h()
    user = h.load_user(update)
    if not await h.require_auth(update, user):
        return None
    return user


def _cmd_name(update: Update) -> str:
    text = update.effective_message.text or ""
    return text.split()[0].lstrip("/").split("@")[0].lower()


async def _open_ca(update, user, context, mode: str) -> bool:
    h = await _h()
    args = context.args or []
    blob = " ".join(args)
    ca = h.extract_ca(blob) or (args[0].strip() if args else "")
    if ca and (h.is_contract(ca) or h.extract_ca(ca)):
        ca = h.extract_ca(ca) or ca
        await h.show_token(update, user, h.guess_chain(ca, user["user_id"]), ca, mode, None)
        return True
    return False


async def _panel(update, query, text, markup):
    h = await _h()
    if query:
        await h.safe_edit(query, text, markup)
    else:
        await h.send_panel(update, text, markup)


async def show_godmode(update, user, query):
    h = await _h()
    items = db.list_snipes(user["user_id"])
    body = (
        "⚡ <b>God Mode</b>\n\n"
        "Pending-tx / method snipes fire the instant liquidity is added. "
        "Same list as Auto Snipe — keep Default Wallet funded.\n\n"
    )
    if not items:
        body += "No pending snipes. Tap ➕ then paste a CA."
    else:
        for it in items:
            mark = "🟢" if it["enabled"] else "🔴"
            body += (
                f"{mark} <b>{it['chain']}</b> <code>{html.escape(it['token'])}</code> "
                f"— {html.escape(str(it['amount']))}\n"
            )
    await _panel(update, query, body, kb.snipe_kb(items, h.enabled(user["user_id"])))


async def show_presale(update, user, query):
    h = await _h()
    db.set_state(user["user_id"], "presale_add", {})
    text = (
        "🚀 <b>Presale / Pinksale snipe</b>\n\n"
        "Arm a buy that fires when the launchpad token lists liquidity.\n\n"
        "Reply: <code>CHAIN CA AMOUNT</code>\n"
        "Example: <code>ETH 0xabc... 0.05</code>\n\n"
        "Or paste a CA and we guess the chain from your enabled list."
    )
    await _panel(update, query, text, kb.extra_hub_kb())


async def show_dca(update, user, query):
    h = await _h()
    orders = [o for o in db.list_orders(user["user_id"]) if (o.get("trigger_type") or "").lower() == "dca"]
    body = (
        "📅 <b>DCA</b>\n\n"
        "Dollar-cost average: buy a fixed native amount on an interval.\n"
        "Reply: <code>CHAIN CA AMOUNT MINUTES</code>\n"
        "Example: <code>ETH 0xabc... 0.05 60</code> (buy 0.05 ETH every 60 minutes).\n\n"
    )
    if not orders:
        body += "No DCA orders yet."
    else:
        for o in orders:
            body += (
                f"• {o['chain']} <code>{html.escape(o['token'])}</code> "
                f"{html.escape(o['amount'])} / {html.escape(o['trigger_value'])}m\n"
            )
    db.set_state(user["user_id"], "dca_add", {})
    await _panel(update, query, body, kb.orders_kb(db.list_orders(user["user_id"])))


async def show_campaigns(update, user, query):
    text = (
        f"🏁 <b>{BOT_NAME} Campaigns</b>\n\n"
        "• 💸 <b>Cashback</b> — 25% of protocol fee, claim as buy credit (/claim).\n"
        "• 🎃 <b>PumpFun</b> — SOL trading rebate panel.\n"
        "• ⭐ <b>Premium</b> — extra wallets, copy slots, trending.\n"
        "• 💰 <b>Referral</b> — up to 25% of invited users' fees.\n"
        "• 🏆 <b>Competition</b> — ranked by fills this round.\n\n"
        "Tap a campaign below."
    )
    await _panel(update, query, text, kb.campaigns_kb())


async def show_competition(update, user, query):
    st = db.trade_stats(user["user_id"])
    text = (
        f"🏆 <b>{BOT_NAME} Competition</b>\n\n"
        "Ranked by on-bot fills this round.\n\n"
        f"Your fills: <b>{st['total']}</b> (buys {st['buys']} · sells {st['sells']})\n"
        f"Your rank: <b>#{st['rank']}</b>\n\n"
        "Paste a CA to trade. Leaderboard updates as trades confirm."
    )
    await _panel(update, query, text, kb.campaigns_kb())


async def cmd_calls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import time as _t

    from bot.config import CALL_CHANNEL, CALL_CHANNEL_URL

    h = await _h()
    user = await _auth(update)
    if not user:
        return
    until = float(user.get("premium_until") or 0)
    active = bool(user.get("premium")) and until > _t.time()
    url = CALL_CHANNEL_URL or (f"https://t.me/{CALL_CHANNEL}" if CALL_CHANNEL else "")
    if not active:
        await h.send_panel(
            update,
            "📣 <b>Call channel is for subscribers.</b>\n\n"
            "Pay /premium — $200 per 30 days from the chain you tap. "
            "Then this command opens the private calls channel and auto-tracks it for Auto Buy.",
            kb.premium_kb(),
        )
        return
    if CALL_CHANNEL:
        try:
            db.add_signal(user["user_id"], CALL_CHANNEL)
        except Exception:
            pass
    body = (
        "📣 <b>Your call channel</b>\n\n"
        + (f"<a href=\"{html.escape(url)}\">{html.escape(url)}</a>\n\n" if url else "Admin has not set CALL_CHANNEL_URL yet.\n\n")
        + "Forward calls here or keep Auto Buy 🟢 — CAs from this channel buy on your Default Wallet."
    )
    await h.send_panel(update, body, kb.signals_kb(h.enabled(user["user_id"])))


async def cmd_copytrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.show_copy(update, user, None)


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.show_signals(update, user, None)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        "⚙️ <b>Global Settings</b>\n\nSelect a chain to customize gas, slippage, Anti-MEV, Degen Mode and more.",
        kb.settings_chain_pick_kb(h.enabled(user["user_id"])),
    )


async def cmd_godmode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _auth(update)
    if not user:
        return
    await show_godmode(update, user, None)


async def cmd_presale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _auth(update)
    if not user:
        return
    await show_presale(update, user, None)


async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _auth(update)
    if not user:
        return
    await show_dca(update, user, None)


async def cmd_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _auth(update)
    if not user:
        return
    await show_campaigns(update, user, None)


async def cmd_competition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _auth(update)
    if not user:
        return
    await show_competition(update, user, None)


async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    moved = db.claim_cashback(user["user_id"])
    if not moved:
        await h.send_panel(
            update,
            "💸 Nothing to claim yet. Trade to earn 25% fee rebates, then /claim.",
            kb.cashback_kb(),
        )
        return
    bits = ", ".join(f"{v} {k}" for k, v in moved.items())
    await h.send_panel(
        update,
        f"💸 Claimed <b>{html.escape(bits)}</b> as fee credit. Your next buy(s) skip protocol tax until that credit is used.",
        kb.cashback_kb(),
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    uid = user["user_id"]
    lines = ["💳 <b>Balances</b>\n"]
    for chain in h.enabled(uid):
        try:
            ov = await wallet_overview(uid, chain)
            lines.append(f"<b>{chain}</b>\n{ov or '—'}")
        except Exception as exc:
            lines.append(f"<b>{chain}</b> — {html.escape(str(exc)[:80])}")
    await h.send_panel(update, "\n\n".join(lines), kb.wallets_chain_pick_kb(h.enabled(uid)))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        "🔑 <b>Export key</b>\n\nOpen a wallet → 🔑 Export Key. The private key is sent in chat — save offline, then DELETE that message.",
        kb.wallets_chain_pick_kb(h.enabled(user["user_id"])),
    )


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    if await _open_ca(update, user, context, "buy"):
        return
    db.set_state(user["user_id"], "await_approve", {})
    await h.send_panel(
        update,
        "✅ <b>Approve</b>\n\nPaste a token CA to approve the DEX router from your default wallet.",
        kb.back_main(),
    )


async def cmd_buysell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    mode = "sell" if _cmd_name(update) == "sell" else "buy"
    if await _open_ca(update, user, context, mode):
        return
    db.set_state(user["user_id"], "await_ca", {"mode": mode})
    await h.send_panel(
        update,
        f"⚡ <b>{'SELL' if mode == 'sell' else 'BUY'} NOW</b>\n\nPaste a token contract address (CA) to open the Token Report.",
        kb.back_main(),
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    if await _open_ca(update, user, context, "buy"):
        return
    db.set_state(user["user_id"], "await_ca", {"mode": "buy"})
    await h.send_panel(
        update,
        "🔎 <b>Scan / Chart</b>\n\nPaste a CA. Tax, honeypot, liquidity and chart links load on the Token Report.",
        kb.back_main(),
    )


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        "🇺🇸🇨🇳 <b>Language</b>\n\nSwitch the bot interface language.",
        kb.language_kb(h.lang_of(user)),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    db.set_state(user["user_id"], None)
    await h.send_panel(update, "❌ Cancelled. Main menu:", kb.main_menu_kb(h.lang_of(user)))


async def cmd_scraper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        "🖥 <b>Scraper</b>\n\nForward call messages that contain a CA into this chat. "
        "Add the source with 📡 Signals → ➕ Add Signal Channel. "
        "If Auto Buy is 🟢 in Global Settings, the Default Wallet buys immediately.\n\n"
        "Desktop scraper: keep this chat open and forward from tracked channels.",
        kb.signals_kb(h.enabled(user["user_id"])),
    )


async def cmd_faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        f"❓ <b>{BOT_NAME} FAQ</b>\n\n"
        "• Paste a token CA to trade.\n"
        "• /wallets — generate or import (never import your main wallet).\n"
        "• /autosnipe /godmode — buy the instant liquidity appears.\n"
        "• /copytrade — copy tracked wallets.\n"
        "• /orders /dca — limit and interval buys.\n"
        "• /cashback /claim — 25% fee rebate as credit.\n"
        "• /premium — extra wallets, copy slots, trending.\n"
        f"• Docs: {DOCS_URL}\n• Support: {SUPPORT_URL}",
        kb.back_main(),
    )


async def cmd_tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        f"📘 <b>Quick start</b>\n\n"
        "1. /start — captcha once.\n"
        "2. /wallets — ♻️ Auto-Generate W1 (save the key offline).\n"
        "3. Fund W1 with native token.\n"
        "4. Paste a CA → Token Report → Buy.\n"
        "5. Optional: /copytrade, /autosnipe, /orders, /bridge.\n\n"
        f"Docs: {DOCS_URL}",
        kb.back_main(),
    )


async def cmd_docs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    await h.send_panel(
        update,
        f"📚 <b>Documentation</b>\n\n<a href=\"{DOCS_URL}\">{DOCS_URL}</a>\nSupport: {SUPPORT_URL}",
        kb.back_main(),
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = h.load_user(update)
    up = int(time.time() - BOOT_TS)
    await h.send_panel(
        update,
        f"{BOT_NAME} is running\nuptime {up}s · /status · /help",
        kb.back_main() if user.get("verified") else None,
    )


async def cmd_chain_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    h = await _h()
    user = await _auth(update)
    if not user:
        return
    name = _cmd_name(update).upper()
    if name not in CHAINS:
        await h.send_panel(update, "Unknown chain. /chains")
        return
    await h.show_wallets_chain(update, user, name, None)


async def cmd_syncmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: force re-register the side Menu commands (fixes missing Menu)."""
    import os

    h = await _h()
    user = h.load_user(update)
    raw = (os.getenv("ADMIN_CHAT_ID") or os.getenv("ADMIN_USER_ID") or "").replace(";", ",")
    admins = {p.strip() for p in raw.split(",") if p.strip()}
    if str(user.get("user_id")) not in admins:
        await h.send_panel(update, "Admin only.")
        return
    try:
        from bot.main import sync_menu_commands

        n = await sync_menu_commands(context.application)
        await h.send_panel(update, f"✅ Menu re-synced ({n}/3 scopes). Kill and reopen the chat if Telegram cached the old menu.")
    except Exception as exc:
        await h.send_panel(update, f"❌ Sync failed: {html.escape(str(exc)[:300])}")


def register_command_handlers(application: Application) -> None:
    from bot import handlers as h

    groups = [
        (["start", "metro", "deluge", "sniper", "menu", "home"], h.cmd_start),
        (["help"], h.cmd_help),
        (["support"], h.cmd_support),
        (["chains", "chain"], h.cmd_chains),
        (["wallets", "wallet"], h.cmd_wallets),
        (["quick"], h.cmd_quick),
        (["settings"], cmd_settings),
        (["monitor"], h.cmd_monitor),
        (["summary"], h.cmd_summary),
        (["autosnipe", "snipe"], h.cmd_autosnipe),
        (["godmode"], cmd_godmode),
        (["presale"], cmd_presale),
        (["dca"], cmd_dca),
        (["copytrade", "copy"], cmd_copytrade),
        (["signals"], cmd_signals),
        (["referral"], h.cmd_referral),
        (["cleartrades"], h.cmd_cleartrades),
        (["orders", "limits", "limit"], h.cmd_orders),
        (["pos", "positions", "pnl"], h.cmd_pos),
        (["mvp"], h.cmd_mvp),
        (["trending", "hot"], h.cmd_trending),
        (["pumpfun"], h.cmd_pumpfun),
        (["bridge"], h.cmd_bridge),
        (["private"], h.cmd_private),
        (["relay"], h.cmd_relay),
        (["debridge"], h.cmd_debridge),
        (["arc"], h.cmd_arc),
        (["premium", "subscribe", "subscription"], h.cmd_premium),
        (["calls", "channel"], cmd_calls),
        (["collect"], h.cmd_collect),
        (["disperse"], h.cmd_disperse),
        (["cashback", "rewards"], h.cmd_cashback),
        (["claim"], cmd_claim),
        (["campaigns", "campaign"], cmd_campaigns),
        (["competition"], cmd_competition),
        (["import"], h.cmd_import),
        (["export"], cmd_export),
        (["approve"], cmd_approve),
        (["buysell", "buy", "sell", "trade"], cmd_buysell),
        (["scan", "chart"], cmd_scan),
        (["language", "lang"], cmd_language),
        (["cancel"], cmd_cancel),
        (["scraper"], cmd_scraper),
        (["faq"], cmd_faq),
        (["tutorial"], cmd_tutorial),
        (["docs"], cmd_docs),
        (["ping", "status"], cmd_ping),
        (["balance", "bal"], cmd_balance),
        (["syncmenu"], cmd_syncmenu),
        (["eth", "sol", "bsc", "base", "arb", "avax", "trx", "ton", "monad", "sonic", "hype", "hood"], cmd_chain_alias),
    ]
    for names, fn in groups:
        application.add_handler(CommandHandler(names, fn))
    application.add_handler(MessageHandler(filters.Regex(r"^/wallets_"), h.cmd_wallets_chain))
    application.add_handler(MessageHandler(filters.Regex(r"^/quick_"), h.cmd_quick))
    application.add_handler(CallbackQueryHandler(h.on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text))
