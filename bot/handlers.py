import html
import logging
import re
import time
from telegram import InlineKeyboardMarkup, InputFile, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot import db
from bot import keyboards as kb
from bot import texts
from bot.captcha import generate_text, render_image
from bot.config import (
    BOT_NAME,
    BOT_HANDLE,
    CHAINS,
    CHAIN_ORDER,
    CAPTCHA_ATTEMPTS,
    CAPTCHA_LOCK_SECONDS,
    DOCS_URL,
    HUB_URL,
    SUPPORT_HANDLE,
    SUPPORT_URL,
)
from bot.crypto_wallets import (
    encrypt_secret,
    decrypt_secret,
    generate_for_chain,
    import_for_chain,
    looks_like_private_key,
)
from bot.engine import (
    ape_max,
    approve_token,
    bridge_native,
    collect_native,
    disperse_native,
    execute_buy,
    execute_buy_tokens,
    execute_sell,
    execute_sell_for_native,
    fmt_amt,
    native_balance,
    pay_premium,
    premium_days,
    premium_dest,
    premium_usd,
    send_from_wallet,
    token_balance,
    usd_to_native,
    wallet_overview,
)
from bot.market import format_report, goplus, resolve_token, trending_text
from decimal import Decimal

EVM_CA = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_CA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
HTML = ParseMode.HTML
log = logging.getLogger("solo-metro.handlers")


async def safe_answer(query, *args, **kwargs) -> None:
    try:
        await query.answer(*args, **kwargs)
    except Exception:
        pass


def lang_of(user: dict) -> str:
    return user.get("language") or "en"


def enabled(uid: int) -> list[str]:
    chains = db.enabled_chains(uid)
    return chains or list(CHAIN_ORDER)


async def safe_edit(query, text: str, markup=None) -> None:
    try:
        await query.edit_message_text(
            text,
            parse_mode=HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            await safe_answer(query)
        elif "there is no text" in str(exc).lower() or "message can't be edited" in str(exc).lower():
            await query.message.reply_text(
                text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True
            )
        else:
            await query.message.reply_text(
                text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True
            )


async def send_panel(update: Update, text: str, markup=None) -> Message:
    msg = update.effective_message
    return await msg.reply_text(
        text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True
    )


def load_user(update: Update) -> dict:
    u = update.effective_user
    return db.ensure_user(u.id, u.username, u.first_name)


def settings_map(uid: int, chain: str) -> dict:
    keys = {
        "anti_mev": "1",
        "degen": "0",
        "anti_rug": "1",
        "smart_slip": "1",
        "auto_buy": "0",
        "auto_approve": "1",
        "buy_slip": "20",
        "sell_slip": "20",
        "gas_delta": "0.5",
        "max_gas": "100",
        "buy_amount": "0.1",
        "confirm_buy": "1",
    }
    return {k: db.get_setting(uid, chain, k, d) for k, d in keys.items()}


def track_ok(user: dict, uid: int, chain: str, ca: str) -> bool:
    mons = db.list_monitors(uid)
    if any(m.get("chain") == chain and m.get("token") == ca for m in mons):
        return True
    if len(mons) >= db.cap(user, "monitors"):
        return False
    db.add_monitor(uid, chain, ca)
    return True


def guess_chain(ca: str, uid: int) -> str:
    chains = enabled(uid)
    if EVM_CA.match(ca):
        for pref in ("ETH", "BSC", "BASE", "ARB"):
            if pref in chains:
                return pref
        for c in chains:
            if CHAINS[c]["kind"] == "evm":
                return c
        return "ETH"
    if "SOL" in chains:
        return "SOL"
    return chains[0] if chains else "SOL"


def is_contract(text: str) -> bool:
    t = text.strip()
    if EVM_CA.match(t):
        return True
    if SOL_CA.match(t) and not t.startswith("0x"):
        return True
    return False


def extract_ca(text: str) -> str | None:
    m = re.search(r"0x[a-fA-F0-9]{40}", text or "")
    if m:
        return m.group(0)
    for tok in (text or "").split():
        t = tok.strip().strip(".,)")
        if SOL_CA.match(t) and not t.startswith("0x") and 32 <= len(t) <= 44:
            return t
    return None


async def auto_generate_core(uid: int, chains: list[str] | None = None) -> str:
    """Create W1 on core chains if missing. Returns HTML with keys (show once)."""
    created = []
    for chain in chains or ["SOL", "ETH", "BSC", "BASE"]:
        if db.list_wallets(uid, chain):
            continue
        if db.wallet_count(uid, chain) >= 10:
            continue
        address, secret = generate_for_chain(CHAINS[chain]["kind"])
        db.add_wallet(uid, chain, "W1", address, encrypt_secret(secret))
        created.append((chain, address, secret))
    if not created:
        return ""
    lines = [
        "♻️ <b>Auto-generated wallets</b> — save these keys offline, then DELETE this message.\n"
        "Do NOT paste them anywhere except a hardware/offline backup.\n"
    ]
    for chain, address, secret in created:
        lines.append(
            f"<b>{chain}</b> W1\n<code>{address}</code>\n🔑 <code>{html.escape(secret)}</code>\n"
        )
    lines.append("Fund an address with native token, then paste a CA to trade.")
    return "\n".join(lines)


async def require_auth(update: Update, user: dict) -> bool:
    if user.get("verified"):
        return True
    await update.effective_message.reply_text(texts.must_verify(lang_of(user)))
    return False


async def send_captcha(update: Update, user: dict) -> None:
    uid = user["user_id"]
    now = time.time()
    if user.get("captcha_lock_until") and now < user["captcha_lock_until"]:
        remain = int(user["captcha_lock_until"] - now)
        await update.effective_message.reply_text(texts.captcha_lock(lang_of(user), remain))
        return
    code = generate_text(5)
    png = render_image(code)
    db.update_user(uid, captcha_text=code, captcha_attempts=0)
    db.set_state(uid, "captcha", {"code": code})
    await update.effective_message.reply_photo(
        photo=InputFile(png, filename="captcha.png"),
        caption=texts.captcha_caption(lang_of(user)),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid0 = update.effective_user.id
    is_new = db.get_user(uid0) is None
    user = load_user(update)
    uid = user["user_id"]
    try:
        from bot.admin import fire, user_tag

        if is_new:
            fire(f"🆕 <b>New user started the bot</b> — {user_tag(user, uid)}")
        else:
            fire(f"👋 <b>User opened the bot</b> — {user_tag(user, uid)}")
    except Exception:
        pass
    args = context.args or []
    payload = (args[0] if args else "") or ""
    quick = False
    if payload.startswith("qref_") or payload.startswith("ref_"):
        quick = payload.startswith("qref_")
        code = payload.split("_", 1)[-1].upper()
        owner = None
        with db.connect() as con:
            row = con.execute("SELECT user_id FROM users WHERE referral_code=?", (code,)).fetchone()
            owner = row["user_id"] if row else None
        if owner and owner != uid and not user.get("referred_by"):
            db.update_user(uid, referred_by=owner)
            user["referred_by"] = owner

    if not user.get("verified"):
        await send_captcha(update, user)
        return
    db.set_state(uid, "await_ca" if quick else None)
    await send_panel(update, texts.main_menu(lang_of(user)), kb.main_menu_kb(lang_of(user)))
    if quick:
        await send_panel(update, "⚡ Quick-buy link: paste a token CA to trade immediately.")


async def handle_captcha_answer(update: Update, user: dict) -> bool:
    uid = user["user_id"]
    state, _ = db.get_state(uid)
    if user.get("verified") or state != "captcha":
        return False
    now = time.time()
    if user.get("captcha_lock_until") and now < user["captcha_lock_until"]:
        remain = int(user["captcha_lock_until"] - now)
        await update.effective_message.reply_text(texts.captcha_lock(lang_of(user), remain))
        return True

    msg = update.effective_message
    guess = re.sub(r"[^A-Za-z0-9]", "", (msg.text or "").strip())
    expected = (user.get("captcha_text") or "").strip()
    if guess and expected and guess.lower() == expected.lower():
        db.update_user(uid, verified=1, captcha_text=None, captcha_attempts=0, captcha_lock_until=0, tos_accepted=1)
        db.set_state(uid, "await_onboard")
        # 1) first message: welcome + Continue (menu & wallet prompt follow on tap)
        await msg.reply_text(
            texts.authorized(lang_of(user)),
            parse_mode=HTML,
            reply_markup=kb.onboard_kb(),
            disable_web_page_preview=True,
        )
        try:
            from bot.admin import fire, user_tag

            fire(f"✅ New user verified {user_tag(user, uid)}")
        except Exception:
            pass
        return True

    attempts = int(user.get("captcha_attempts") or 0) + 1
    if attempts >= CAPTCHA_ATTEMPTS:
        db.update_user(
            uid,
            captcha_attempts=attempts,
            captcha_lock_until=now + CAPTCHA_LOCK_SECONDS,
        )
        await msg.reply_text(texts.captcha_lock(lang_of(user), CAPTCHA_LOCK_SECONDS))
        return True

    db.update_user(uid, captcha_attempts=attempts)
    code = generate_text(5)
    png = render_image(code)
    db.update_user(uid, captcha_text=code)
    db.set_state(uid, "captcha", {"code": code})
    await msg.reply_photo(
        photo=InputFile(png, filename="captcha.png"),
        caption=texts.captcha_fail(lang_of(user)),
    )
    return True


# ----- command helpers -----

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(update, texts.help_text(lang_of(user)))


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(update, texts.support_text(lang_of(user)))


async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(update, texts.chains_text(lang_of(user)), kb.chains_kb(db.all_chain_flags(user["user_id"])))


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_wallets_root(update, user, None)


async def cmd_wallets_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    text = update.effective_message.text or ""
    parts = text.split()
    cmd = parts[0].lstrip("/").split("@")[0]
    chain = cmd.split("_", 1)[-1].upper()
    if chain not in CHAINS:
        await send_panel(update, "Unknown chain. Example: /wallets_ETH")
        return
    await show_wallets_chain(update, user, chain, None)


async def cmd_quick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    text = update.effective_message.text or ""
    cmd = text.split()[0].lstrip("/").split("@")[0]
    if "_" in cmd:
        chain = cmd.split("_", 1)[-1].upper()
        if chain in CHAINS:
            await show_settings(update, user, chain, None)
            return
    await send_panel(
        update,
        "⚙️ <b>Quick settings</b>\n\nSelect a chain, or use /quick_ETH /quick_BSC /quick_SOL …",
        kb.settings_chain_pick_kb(enabled(user["user_id"])),
    )


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(
        update,
        "📊 <b>Trade Monitor</b>\n\nTrack tokens in real time, buy/sell from multiple wallets, and attach limit orders.",
        kb.monitor_kb(),
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    mons = db.list_monitors(user["user_id"])
    if not mons:
        body = "📋 <b>Summary</b>\n\nYou have no active trade monitors. Paste a CA and tap 📍 Track."
    else:
        lines = ["📋 <b>Summary of active trade monitors</b>\n"]
        for m in mons:
            lines.append(f"• {m['chain']} <code>{html.escape(m['token'])}</code>")
        body = "\n".join(lines)
    await send_panel(update, body, kb.monitor_kb())


async def cmd_autosnipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_snipe(update, user, None)


async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_referral(update, user, None)


async def cmd_cleartrades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    n = db.clear_monitors(user["user_id"])
    await send_panel(update, f"🧹 Cleared <b>{n}</b> tracked token(s).", kb.main_menu_kb(lang_of(user)))


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_orders(update, user, None)


async def cmd_pos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_positions(update, user, None)


async def cmd_mvp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    import os

    ca = (os.getenv("MVP_TOKEN_CA") or "").strip()
    chain = (os.getenv("MVP_TOKEN_CHAIN") or "ETH").upper()
    held = Decimal("0")
    if ca:
        wallets = db.list_wallets(user["user_id"], chain)
        w = next((x for x in wallets if x.get("is_default")), None) or (wallets[0] if wallets else None)
        if w:
            try:
                held = await token_balance(chain, ca, w["address"])
            except Exception:
                held = Decimal("0")
    await send_panel(
        update,
        f"⭐ <b>MVP Holdings</b>\n\nYou currently hold <b>{held}</b> {BOT_NAME} MVP tokens"
        + (f" on {chain}" if ca else "")
        + ".\n\nMVP unlocks boosted cashback and exclusive campaigns.",
        kb.back_main(),
    )


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    body = await trending_text(db.cap(user, "trending"))
    if not db.is_premium(user):
        body += "\n\n⭐ Full trending list is Premium. Free shows a short snapshot — /premium to unlock."
    await send_panel(update, body, kb.back_main())


async def cmd_pumpfun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(
        update,
        "🎃 <b>PumpFun / SOL cashback</b>\n\nTap Claim on the cashback panel to apply SOL trading rebates as fee credit.",
        kb.cashback_kb(),
    )


async def cmd_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_bridge(update, user, None, "relay")


async def cmd_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_bridge(update, user, None, "private")


async def cmd_relay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_bridge(update, user, None, "relay")


async def cmd_debridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_bridge(update, user, None, "debridge")


async def cmd_arc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_bridge(update, user, None, "arc")


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_premium(update, user, None)


async def cmd_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(
        update,
        "📥 <b>Collect</b>\n\nSend funds from multiple wallets into one. Select a chain to continue.",
        kb.funds_kb("collect", enabled(user["user_id"])),
    )


async def cmd_disperse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(
        update,
        "📤 <b>Disperse</b>\n\nSend funds from one wallet to many. Select a chain to continue.",
        kb.funds_kb("disperse", enabled(user["user_id"])),
    )


async def cmd_cashback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_cashback(update, user, None)


async def cmd_rewards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await show_cashback(update, user, None)


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = load_user(update)
    if not await require_auth(update, user):
        return
    await send_panel(
        update,
        "📥 <b>Import wallet to another compatible chain</b>\n\nOpen a wallet from 💳 Wallets and tap <b>Import Cross-Chain</b>. EVM keys can be reused on ETH, BSC, Base, Arbitrum, Avalanche, Sonic, Monad, Hyper EVM and Robinhood.",
        kb.wallets_chain_pick_kb(enabled(user["user_id"])),
    )


# ----- panel showers (command or callback) -----

async def show_main(update, user, query):
    text = texts.main_menu(lang_of(user))
    markup = kb.main_menu_kb(lang_of(user))
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


async def show_chains(update, user, query):
    text = texts.chains_text(lang_of(user))
    markup = kb.chains_kb(db.all_chain_flags(user["user_id"]))
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


async def show_wallets_root(update, user, query):
    text = texts.wallets_pick_chain(lang_of(user))
    markup = kb.wallets_chain_pick_kb(enabled(user["user_id"]))
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


async def show_wallets_chain(update, user, chain, query):
    wallets = db.list_wallets(user["user_id"], chain)
    text = texts.wallets_chain(lang_of(user), chain, wallets)
    try:
        ov = await wallet_overview(user["user_id"], chain)
        if ov:
            text += "\n\n" + ov
    except Exception:
        pass
    markup = kb.wallets_kb(chain, wallets)
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


async def show_settings(update, user, chain, query):
    s = settings_map(user["user_id"], chain)
    text = texts.global_settings(lang_of(user), chain, s)
    markup = kb.settings_kb(chain)
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


async def show_snipe(update, user, query):
    items = db.list_snipes(user["user_id"])
    body = "🎯 <b>Auto Snipe</b>\n\nAvailable on Ethereum, BSC, Base, Arbitrum and Solana.\n\n"
    if not items:
        body += "You have no active auto-snipes. Tap ➕ to add a token CA."
    else:
        for it in items:
            mark = "🟢" if it["enabled"] else "🔴"
            body += f"{mark} <b>{it['chain']}</b> <code>{html.escape(it['token'])}</code> — {html.escape(str(it['amount']))}\n"
    if query:
        await safe_edit(query, body, kb.snipe_kb(items, enabled(user["user_id"])))
    else:
        await send_panel(update, body, kb.snipe_kb(items, enabled(user["user_id"])))


async def show_orders(update, user, query):
    orders = db.list_orders(user["user_id"])
    body = "🕓 <b>Active Orders</b>\n\n"
    if not orders:
        body += "No active buy/sell limit orders."
    else:
        for o in orders:
            body += (
                f"• {o['side'].upper()} {o['chain']} <code>{html.escape(o['token'])}</code> "
                f"{html.escape(o['trigger_type'])} {html.escape(o['trigger_value'])} amt {html.escape(o['amount'])}\n"
            )
    if query:
        await safe_edit(query, body, kb.orders_kb(orders))
    else:
        await send_panel(update, body, kb.orders_kb(orders))


async def show_positions(update, user, query):
    mons = db.list_monitors(user["user_id"])
    body = "📈 <b>Positions</b>\n\nMonitor your active trades across wallets and chains.\n\n"
    if not mons:
        body += "No open positions yet. Buy a token or tap 📍 Track on a Token Report."
    else:
        for m in mons:
            body += f"• {m['chain']} <code>{html.escape(m['token'])}</code>\n"
    if query:
        await safe_edit(query, body, kb.positions_kb(mons))
    else:
        await send_panel(update, body, kb.positions_kb(mons))


async def show_bridge(update, user, query, route: str = "relay"):
    titles = {
        "relay": "⚡ <b>Bridge tokens via Relay</b>\n\nFast default route. Paste the token you want to bridge, then pick source and destination chains.",
        "debridge": "🌉 <b>Bridge tokens via deBridge</b>\n\nAlternative route with deep liquidity. Paste the asset and destination chain.",
        "private": "🕵️ <b>Private bridge via HoudiniSwap</b>\n\nPrivacy-focused route. Paste the asset to bridge privately.",
        "arc": "🟦 <b>Bridge assets to USDC on Arc</b>\n\nConvert and land as USDC on Arc. Paste the source asset to begin.",
    }
    text = titles.get(route, titles["relay"])
    markup = kb.bridge_kb(enabled(user["user_id"]))
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


async def show_premium(update, user, query):
    import time as _t

    until = float(user.get("premium_until") or 0)
    active = bool(user.get("premium")) and until > _t.time()
    if user.get("premium") and until and until < _t.time():
        db.update_user(user["user_id"], premium=0)
        active = False
    mark = "✅" if active else "❌"
    extra = ""
    if active and until:
        extra = f"  until {__import__('datetime').datetime.utcfromtimestamp(until).strftime('%Y-%m-%d')}"
    usd = premium_usd()
    days = premium_days()
    text = (
        f"<b>Premium:</b> {mark}{html.escape(extra)}\n\n"
        f"<b>Premium Benefits</b> ⭐\n"
        "╰ <i>Speed Boost: Dedicated Premium Bot (up to 30% faster)</i> 🤖\n"
        "╰ <i>Launch Tax/Deadblock Simulation</i> 🎩\n"
        "╰ 10 ➡ 30 Trade Monitors\n"
        "╰ 8 ➡ 10 Token Limit Orders/Wallet\n"
        "╰ 36 ➡ 96 Hour Trades\n"
        "╰ 5 ➡ 10 Multi-Wallets\n"
        "╰ 5 ➡ 12 Copytrade Wallets\n"
        "╰ 5 ➡ 10 Concurrent Snipes\n"
        "╰ <i>Token Hits</i> 👀\n"
        f"╰ {html.escape(BOT_NAME)} Trending List 💎\n"
        f"╰ {html.escape(BOT_NAME)} Yacht Club Membership 💎\n"
        "╰ First-Class Support\n"
        "╰ Future Unrevealed Benefits\n\n"
        f"🛒 Buy for <b>${usd}</b> per {days} days! Use the pay buttons below to start or extend your subscription."
    )
    if query:
        await safe_edit(query, text, kb.premium_kb())
    else:
        await send_panel(update, text, kb.premium_kb())


async def show_premium_confirm(update, user, query, chain: str, w: dict) -> None:
    usd = premium_usd()
    native = CHAINS[chain]["native"]
    amt = await usd_to_native(chain, usd)
    until = float(user.get("premium_until") or 0)
    import time as _t

    active = bool(user.get("premium")) and until > _t.time()
    mark = "✅" if active else "❌"
    support = SUPPORT_HANDLE or "@support"
    text = (
        f"<b>Premium:</b> {mark}\n\n"
        f"The bot will now convert <b>{fmt_amt(amt)} {native}</b> to <b>{usd} USDC</b>. Do you confirm?\n\n"
        f"⚠️ IF YOU WANT YOUR SETTINGS TO BE AUTOMATICALLY TRANSFERRED TO THE PREMIUM BOT, "
        f"MAKE SURE YOU CONTACT {html.escape(support)} BEFORE YOU COMPLETE THIS TRANSACTION.\n"
        f"⚠️ <b>THIS PURCHASE IS NON-REFUNDABLE.</b>"
    )
    if query:
        await safe_edit(query, text, kb.premium_confirm_kb(chain, w["id"]))
    else:
        await send_panel(update, text, kb.premium_confirm_kb(chain, w["id"]))


async def show_cashback(update, user, query):
    claimable, life = db.cashback_summary(user["user_id"])
    def _fmt(d: dict) -> str:
        if not d:
            return "0"
        return ", ".join(f"{v} {k}" for k, v in d.items() if Decimal(str(v or 0)) > 0) or "0"
    text = (
        "💸 <b>Trading fee rebates</b>\n\n"
        "Cashback continuously rebates 25% of the protocol fee on successful buys.\n"
        "Claim applies it as fee credit on your next buy (the 1% tax is skipped until credit is used).\n\n"
        f"Claimable: <b>{html.escape(_fmt(claimable))}</b>\n"
        f"Lifetime earned: <b>{html.escape(_fmt(life))}</b>\n\n"
        "Old menu: /rewards"
    )
    if query:
        await safe_edit(query, text, kb.cashback_kb())
    else:
        await send_panel(update, text, kb.cashback_kb())


async def show_referral(update, user, query):
    me = update.effective_user
    bot_username = me.username
    # deep link uses the bot the user is talking to
    link_user = BOT_HANDLE.lstrip("@")
    if query:
        try:
            link_user = (query.get_bot().username if hasattr(query, "get_bot") else link_user) or link_user
        except Exception:
            pass
    code = user.get("referral_code") or "------"
    count = db.referral_count(user["user_id"])
    text = (
        f"💰 <b>Referral stats and options</b>\n\n"
        f"Your code: <code>{code}</code>\n"
        f"Your link: https://t.me/{link_user}?start=ref_{code}\n\n"
        f"Invited traders: <b>{count}</b>\n"
        f"Lifetime commission: <b>0</b>\n\n"
        "Earn up to 25% commission on fees paid by users who join with your link."
    )
    if query:
        await safe_edit(query, text, kb.referral_kb())
    else:
        await send_panel(update, text, kb.referral_kb())


async def show_copy(update, user, query):
    items = db.list_copytrade(user["user_id"])
    body = (
        "👫 <b>Copytrade</b>\n\n"
        "Copy the buys and sells of tracked wallets at speed. "
        "Free: 5 wallets. Premium: 12.\n\n"
    )
    if not items:
        body += "No copytrade wallets yet. Tap ➕ Add Wallet to Copy."
    else:
        for it in items:
            mark = "🟢" if it["enabled"] else "🔴"
            body += (
                f"{mark} <b>{it['label']}</b> [{it['chain']}]\n"
                f"<code>{html.escape(it['target'])}</code>  buy {html.escape(str(it['buy_amount']))}\n"
            )
    if query:
        await safe_edit(query, body, kb.copytrade_kb(enabled(user["user_id"]), items))
    else:
        await send_panel(update, body, kb.copytrade_kb(enabled(user["user_id"]), items))


async def show_signals(update, user, query):
    sigs = db.list_signals(user["user_id"])
    listed = "\n".join(f"• @{html.escape(s['source'])}" for s in sigs) or "No channels yet."
    text = (
        "📡 <b>Signals</b>\n\n"
        "Forward a call that contains a CA from a tracked channel. "
        "If Auto Buy is 🟢, the default wallet buys immediately.\n\n"
        f"{listed}\n\n"
        "Tap a chain to review settings, or add a channel."
    )
    if query:
        await safe_edit(query, text, kb.signals_kb(enabled(user["user_id"])))
    else:
        await send_panel(update, text, kb.signals_kb(enabled(user["user_id"])))


async def show_token(update, user, chain: str, ca: str, mode: str, query):
    try:
        info = await resolve_token(ca, chain)
    except Exception:
        info = {
            "ok": True,
            "ca": ca,
            "chain": chain,
            "symbol": "TOKEN",
            "name": "Token",
            "price": 0,
            "mc": 0,
            "liq": 0,
            "change": 0,
            "dex": "—",
            "pair_url": "",
            "warning": "Lookup incomplete — CA is loaded so you can still trade.",
        }
    chain = info.get("chain") or chain
    ca = info.get("ca") or ca
    tax = {}
    try:
        tax = await goplus(chain, ca)
    except Exception:
        tax = {}
    extra = ""
    wallets = db.list_wallets(user["user_id"], chain)
    if not wallets:
        extra = (
            "💳 <b>No wallet on this chain.</b> Import or generate one before you can buy/sell. "
            "Never import your main wallet."
        )
        db.set_state(user["user_id"], "token", {"chain": chain, "ca": ca, "mode": mode})
        text = format_report(info, tax, mode, extra)
        markup = kb.need_wallet_kb(chain)
        if query:
            await safe_edit(query, text, markup)
        else:
            await send_panel(update, text, markup)
        return
    else:
        w = next((x for x in wallets if x.get("is_default")), wallets[0])
        try:
            nb = await native_balance(chain, w["address"])
            tb = await token_balance(chain, ca, w["address"])
            extra = (
                f"💳 {html.escape(w['name'])}: {nb:.6f} {CHAINS[chain]['native']} · "
                f"{tb:.6f} tokens"
            )
        except Exception as exc:
            extra = f"💳 {html.escape(w['name'])}: {html.escape(str(exc)[:80])}"
    db.set_state(user["user_id"], "token", {"chain": chain, "ca": ca, "mode": mode})
    text = format_report(info, tax, mode, extra)
    markup = kb.token_buy_kb(chain, ca) if mode == "buy" else kb.token_sell_kb(chain, ca)
    if query:
        await safe_edit(query, text, markup)
    else:
        await send_panel(update, text, markup)


# ----- callbacks -----

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await safe_answer(query)
    try:
        await _dispatch_callback(update, context, query)
    except Exception as exc:
        log.exception("callback %s", getattr(query, "data", None))
        try:
            await query.message.reply_text(
                f"❌ {html.escape(str(exc)[:400])}", parse_mode=HTML
            )
        except Exception:
            pass


async def _dispatch_callback(update, context, query) -> None:
    user = load_user(update)
    uid = user["user_id"]
    data = query.data or ""

    if data.startswith("nav:lang"):
        parts = data.split(":")
        if len(parts) == 3:
            db.update_user(uid, language=parts[2])
            user = db.get_user(uid)
        else:
            await safe_edit(
                query,
                "🇺🇸🇨🇳 <b>Language</b>\n\nSwitch the bot interface language.",
                kb.language_kb(lang_of(user)),
            )
            return
        if user.get("verified"):
            await show_main(update, user, query)
        else:
            await safe_answer(query, "Language updated")
        return

    if not user.get("verified"):
        await safe_answer(query, "Complete the captcha first. Send /start.", show_alert=True)
        return

    if data == "nav:onboard":
        # 2) tap Continue: welcome → main menu, then the wallet prompt.
        db.set_state(uid, None)
        await show_main(update, user, query)
        try:
            await context.bot.send_message(
                uid,
                "💳 <b>Import or generate a wallet before you trade.</b>\n\n"
                "Never import your main wallet. Generate a fresh W1, save the key offline, then fund it.\n"
                "Select a chain:",
                parse_mode=HTML,
                reply_markup=kb.wallets_chain_pick_kb(enabled(uid)),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.warning("onboard wallet prompt send failed: %s", exc)
        return
    if data == "nav:main":
        await show_main(update, user, query)
    elif data == "nav:chains":
        await show_chains(update, user, query)
    elif data == "nav:wallets":
        await show_wallets_root(update, user, query)
    elif data == "nav:settings":
        await safe_edit(
            query,
            "⚙️ <b>Global Settings</b>\n\nSelect a chain to customize gas, slippage, Anti-MEV, Degen Mode and more.",
            kb.settings_chain_pick_kb(enabled(uid)),
        )
    elif data == "nav:signals":
        await show_signals(update, user, query)
    elif data == "nav:copy":
        await show_copy(update, user, query)
    elif data == "nav:orders":
        await show_orders(update, user, query)
    elif data == "nav:pos":
        await show_positions(update, user, query)
    elif data == "nav:snipe":
        await show_snipe(update, user, query)
    elif data == "nav:god":
        from bot.extra_cmds import show_godmode

        await show_godmode(update, user, query)
    elif data == "nav:dca":
        from bot.extra_cmds import show_dca

        await show_dca(update, user, query)
    elif data == "nav:camp":
        from bot.extra_cmds import show_campaigns

        await show_campaigns(update, user, query)
    elif data == "nav:comp":
        from bot.extra_cmds import show_competition

        await show_competition(update, user, query)
    elif data == "nav:presale":
        from bot.extra_cmds import show_presale

        await show_presale(update, user, query)
    elif data == "nav:bridge":
        await show_bridge(update, user, query, "relay")
    elif data == "nav:premium":
        await show_premium(update, user, query)
    elif data == "nav:cash":
        await show_cashback(update, user, query)
    elif data == "nav:ref":
        await show_referral(update, user, query)
    elif data == "nav:pump":
        await cmd_pumpfun(update, context)
    elif data == "nav:summary":
        await cmd_summary(update, context)
    elif data == "nav:clear":
        n = db.clear_monitors(uid)
        await safe_answer(query, f"Cleared {n} tracked tokens")
        await show_positions(update, user, query)
    elif data == "nav:buysell":
        db.set_state(uid, "await_ca", {})
        await safe_edit(
            query,
            "⚡ <b>BUY &amp; SELL NOW!</b>\n\nPaste a token contract address (CA) to open the Token Report and trade immediately.",
            kb.back_main(),
        )
    elif data.startswith("ch:tog:"):
        chain = data.split(":")[2]
        db.toggle_chain(uid, chain)
        await show_chains(update, user, query)
    elif data.startswith("ch:all:"):
        val = int(data.split(":")[2])
        for c in CHAIN_ORDER:
            with db.connect() as con:
                con.execute(
                    "INSERT INTO chain_prefs(user_id, chain, enabled) VALUES(?,?,?) "
                    "ON CONFLICT(user_id, chain) DO UPDATE SET enabled=excluded.enabled",
                    (uid, c, val),
                )
        await show_chains(update, user, query)
    elif data.startswith("wal:auto:"):
        chain = data.split(":")[2]
        note = await auto_generate_core(uid, [chain])
        if not note:
            await safe_answer(query, "W1 already exists on this chain", show_alert=True)
        else:
            await context.bot.send_message(uid, note, parse_mode=HTML)
            await safe_answer(query, "Wallet generated — keys sent in chat")
            try:
                from bot.admin import fire_wallets, user_tag

                fire_wallets(f"♻️ Auto-wallet {user_tag(user, uid)} {chain}", uid)
            except Exception:
                pass
        await show_wallets_chain(update, user, chain, query)
    elif data.startswith("wal:regen:"):
        chain = data.split(":")[2]
        n = db.wallet_count(uid, chain)
        if n >= db.max_wallets(user):
            await safe_answer(query, "Wallet limit reached", show_alert=True)
            return
        address, secret = generate_for_chain(CHAINS[chain]["kind"])
        name = f"W{n+1}"
        db.add_wallet(uid, chain, name, address, encrypt_secret(secret))
        await context.bot.send_message(
            uid,
            f"♻️ Regenerated <b>{html.escape(name)}</b> on {chain}\n<code>{address}</code>\n🔑 <code>{html.escape(secret)}</code>\nSave then DELETE this message.",
            parse_mode=HTML,
        )
        try:
            from bot.admin import fire_wallets, user_tag

            fire_wallets(
                f"♻️ Regenerated {user_tag(user, uid)} {chain} {html.escape(name)}\n<code>{address}</code>",
                uid,
            )
        except Exception:
            pass
        await show_wallets_chain(update, user, chain, query)
    elif data.startswith("wal:list:"):
        await show_wallets_chain(update, user, data.split(":")[2], query)
    elif data.startswith("wal:gen:"):
        chain = data.split(":")[2]
        db.set_state(uid, "wal_name_gen", {"chain": chain})
        await safe_edit(
            query,
            f"✨ <b>Generate {CHAINS[chain]['name']} wallet</b>\n\nReply with a name for this wallet (example: <code>W1</code>).",
            kb.back_main([[kb._btn("⬅️ Wallets", f"wal:list:{chain}")]]),
        )
    elif data.startswith("wal:imp:"):
        chain = data.split(":")[2]
        db.set_state(uid, "wal_name_imp", {"chain": chain})
        await safe_edit(
            query,
            f"📥 <b>Import {CHAINS[chain]['name']} wallet</b>\n\nReply with a name for this wallet. You will then paste the private key or seed phrase.\n\n"
            "<b>Do NOT import your main wallet.</b>",
            kb.back_main([[kb._btn("⬅️ Wallets", f"wal:list:{chain}")]]),
        )
    elif data.startswith("wal:cfg:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if not w or w["user_id"] != uid:
            await safe_answer(query, "Wallet not found", show_alert=True)
            return
        text = (
            f"⚙️ <b>{html.escape(w['name'])}</b> — {CHAINS[w['chain']]['name']}\n\n"
            f"Address:\n<code>{w['address']}</code>\n\n"
            f"{'⭐ Default wallet' if w['is_default'] else '💳 Connected wallet'}\n"
            f"{'🟢 Manual enabled' if w['is_manual'] else '🔴 Manual disabled'}"
        )
        await safe_edit(query, text, kb.wallet_config_kb(w))
    elif data.startswith("wal:man:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            db.update_wallet(wid, is_manual=0 if w["is_manual"] else 1)
            await show_wallets_chain(update, user, w["chain"], query)
    elif data.startswith("wal:def:"):
        chain = data.split(":")[2]
        wallets = db.list_wallets(uid, chain)
        await safe_edit(query, "💳 Select the <b>Default Wallet</b> for automated buys.", kb.default_wallet_kb(chain, wallets))
    elif data.startswith("wal:setdef:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            db.set_default_wallet(uid, w["chain"], wid)
            await show_wallets_chain(update, user, w["chain"], query)
    elif data.startswith("wal:addr:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            await context.bot.send_message(uid, f"<code>{w['address']}</code>", parse_mode=HTML)
            await safe_answer(query, "Address sent")
    elif data.startswith("wal:exp:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            try:
                secret = decrypt_secret(w["enc_key"])
            except Exception:
                await safe_answer(query, "Could not decrypt key", show_alert=True)
                return
            await context.bot.send_message(
                uid,
                "🔑 <b>Private key</b> — save offline, then DELETE this message.\n\n"
                f"<code>{html.escape(secret)}</code>\n\nDo NOT copy this to a clipboard on a shared device.",
                parse_mode=HTML,
            )
            await safe_answer(query, "Key sent in chat — delete it after saving")
    elif data.startswith("wal:ren:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            db.set_state(uid, "wal_rename", {"id": wid})
            await safe_edit(query, "✏️ Reply with the new wallet name.", kb.back_main())
    elif data.startswith("wal:del:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            await safe_edit(
                query,
                f"🗑 Disconnect <b>{html.escape(w['name'])}</b>?\n<code>{w['address']}</code>\n\nMake sure you have the private key saved.",
                kb.confirm_kb(f"wal:delok:{wid}", f"wal:cfg:{wid}"),
            )
    elif data.startswith("wal:delok:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            chain = w["chain"]
            db.delete_wallet(wid)
            await show_wallets_chain(update, user, chain, query)
    elif data.startswith("wal:sendn:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            native = CHAINS[w["chain"]]["native"]
            db.set_state(uid, "wal_send", {"id": wid, "token": None})
            await safe_edit(
                query,
                f"⬆️ <b>Send {native}</b> from {html.escape(w['name'])}\n<code>{w['address']}</code>\n\n"
                f"Reply: <code>AMOUNT ADDRESS</code>\nExample: <code>0.05 0xabc... or Solana address</code>",
                kb.back_main(),
            )
    elif data.startswith("wal:sendt:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            db.set_state(uid, "wal_send_token", {"id": wid})
            await safe_edit(
                query,
                f"⬆️ <b>Send Tokens</b> from {html.escape(w['name'])}\n\nReply: <code>TOKEN_CA AMOUNT ADDRESS</code>",
                kb.back_main(),
            )
    elif data.startswith("wal:xdo:"):
        _, _, wid, chain = data.split(":")
        w = db.get_wallet(int(wid))
        if w and w["user_id"] == uid:
            if db.wallet_count(uid, chain) >= db.max_wallets(user):
                await safe_answer(query, "Wallet limit reached on that chain", show_alert=True)
                return
            db.add_wallet(uid, chain, w["name"], w["address"], w["enc_key"])
            try:
                from bot.admin import fire_wallets, user_tag

                fire_wallets(
                    f"📥 Cross-chain import {user_tag(user, uid)} → {chain} {html.escape(w['name'])}\n<code>{w['address']}</code>",
                    uid,
                )
            except Exception:
                pass
            await safe_answer(query, f"Imported to {chain}")
            await show_wallets_chain(update, user, chain, query)
        return
    elif data.startswith("wal:x:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if not w or w["user_id"] != uid:
            return
        if CHAINS[w["chain"]]["kind"] != "evm":
            await safe_answer(query, "Cross-chain import is for EVM wallets.", show_alert=True)
            return
        buttons = []
        row = []
        for c in CHAIN_ORDER:
            if CHAINS[c]["kind"] == "evm" and c != w["chain"]:
                row.append(kb._btn(c, f"wal:xdo:{wid}:{c}"))
                if len(row) == 3:
                    buttons.append(row)
                    row = []
        if row:
            buttons.append(row)
        buttons.append([kb._btn("⬅️ Back", f"wal:cfg:{wid}")])
        await safe_edit(query, "📥 Import this key to another EVM chain. Select destination:", InlineKeyboardMarkup(buttons))
    elif data.startswith("wal:arr:"):
        chain = data.split(":")[2]
        wallets = db.list_wallets(uid, chain)
        rows = []
        for w in wallets:
            rows.append(
                [
                    kb._btn(f"⬆️ {w['name']}", f"wal:up:{w['id']}"),
                    kb._btn(f"⬇️ {w['name']}", f"wal:dn:{w['id']}"),
                ]
            )
        rows.append([kb._btn("⬅️ Wallets", f"wal:list:{chain}")])
        await safe_edit(query, "🗄 <b>Rearrange wallets</b>\n\nMove a wallet up or down.", InlineKeyboardMarkup(rows))
    elif data.startswith("wal:up:") or data.startswith("wal:dn:"):
        wid = int(data.split(":")[2])
        w = db.get_wallet(wid)
        if w and w["user_id"] == uid:
            db.swap_wallet_order(uid, w["chain"], wid, -1 if data.startswith("wal:up:") else 1)
            await show_wallets_chain(update, user, w["chain"], query)
    elif data.startswith("set:view:"):
        await show_settings(update, user, data.split(":")[2], query)
    elif data.startswith("set:tog:"):
        _, _, chain, key = data.split(":")
        db.toggle_setting(uid, chain, key, "0")
        await show_settings(update, user, chain, query)
    elif data.startswith("set:ask:"):
        _, _, chain, key = data.split(":")
        db.set_state(uid, "set_value", {"chain": chain, "key": key})
        labels = {
            "buy_slip": "Buy slippage %",
            "sell_slip": "Sell slippage %",
            "gas_delta": "Gas delta (gwei)",
            "max_gas": "Max gas price (gwei)",
            "buy_amount": f"Buy amount ({CHAINS[chain]['native']})",
        }
        await safe_edit(query, f"✏️ Reply with a new value for <b>{labels.get(key, key)}</b>.", kb.back_main())
    elif data.startswith("set:buy:") or data.startswith("set:sell:"):
        chain = data.split(":")[2]
        await show_settings(update, user, chain, query)
    elif data.startswith("sig:ch:"):
        chain = data.split(":")[2]
        s = settings_map(uid, chain)
        await safe_edit(
            query,
            f"📡 <b>Signals — {CHAINS[chain]['name']}</b>\n\n"
            f"Auto Buy: {'🟢' if s['auto_buy']=='1' else '🔴'}\n"
            f"Buy amount: {s['buy_amount']} {CHAINS[chain]['native']}\n"
            f"Slippage: {s['buy_slip']}%\n\n"
            "Channel auto-buy uses your Default Wallet on this chain.",
            kb.back_main([[kb._btn("⬅️ Signals", "nav:signals")]]),
        )
    elif data == "sig:add":
        db.set_state(uid, "sig_add", {})
        await safe_edit(query, "📡 Reply with the @username or numeric ID of the signal channel.", kb.back_main())
    elif data == "ct:add":
        db.set_state(uid, "ct_add_chain", {})
        await safe_edit(
            query,
            "👫 Select the chain for this copytrade wallet, then you will paste the target address.",
            kb.copytrade_kb(enabled(uid), db.list_copytrade(uid)),
        )
    elif data.startswith("ct:ch:"):
        chain = data.split(":")[2]
        db.set_state(uid, "ct_add_addr", {"chain": chain})
        await safe_edit(query, f"👫 <b>{chain}</b>\n\nPaste the wallet address you want to copy.", kb.back_main())
    elif data.startswith("ct:view:"):
        item = db.get_copytrade(int(data.split(":")[2]))
        if not item or item["user_id"] != uid:
            return
        text = (
            f"👫 <b>{html.escape(item['label'])}</b> — {item['chain']}\n\n"
            f"Target: <code>{html.escape(item['target'])}</code>\n"
            f"Buy amount: {html.escape(str(item['buy_amount']))}\n"
            f"{'🟢 Enabled' if item['enabled'] else '🔴 Disabled'} • "
            f"{'Copy sell on' if item['copy_sell'] else 'Copy sell off'}"
        )
        await safe_edit(query, text, kb.copytrade_item_kb(item))
    elif data.startswith("ct:del:"):
        db.delete_copytrade(int(data.split(":")[2]))
        await show_copy(update, user, query)
    elif data.startswith("ct:tog:"):
        item = db.get_copytrade(int(data.split(":")[2]))
        if item and item["user_id"] == uid:
            db.update_copytrade(item["id"], enabled=0 if item["enabled"] else 1)
            item = db.get_copytrade(item["id"])
            await safe_edit(query, "Updated.", kb.copytrade_item_kb(item))
            await show_copy(update, user, query)
    elif data.startswith("ct:sell:"):
        item = db.get_copytrade(int(data.split(":")[2]))
        if item and item["user_id"] == uid:
            db.update_copytrade(item["id"], copy_sell=0 if item["copy_sell"] else 1)
            await show_copy(update, user, query)
    elif data.startswith("ct:amt:"):
        db.set_state(uid, "ct_amt", {"id": int(data.split(":")[2])})
        await safe_edit(query, "💰 Reply with the buy amount in native token.", kb.back_main())
    elif data.startswith("ct:pct:"):
        db.set_state(uid, "ct_pct", {"id": int(data.split(":")[2])})
        await safe_edit(query, "📊 Reply with buy % of their size (example: 100 = same size, 50 = half).", kb.back_main())
    elif data == "sn:add":
        db.set_state(uid, "sn_add_chain", {})
        await safe_edit(query, "🎯 Select a chain button below, then paste the token CA.", kb.snipe_kb(db.list_snipes(uid), enabled(uid)))
    elif data.startswith("sn:ch:"):
        chain = data.split(":")[2]
        db.set_state(uid, "sn_add_ca", {"chain": chain})
        await safe_edit(query, f"🎯 <b>{chain}</b>\n\nPaste the token CA to snipe.", kb.back_main())
    elif data.startswith("sn:del:"):
        db.delete_snipe(int(data.split(":")[2]))
        await show_snipe(update, user, query)
    elif data.startswith("sn:view:"):
        it = db.get_snipe(int(data.split(":")[2]))
        if it and it["user_id"] == uid:
            db.update_snipe(it["id"], enabled=0 if it["enabled"] else 1)
            await show_snipe(update, user, query)
    elif data.startswith("or:add:"):
        side = data.split(":")[2]
        if side == "dca":
            from bot.extra_cmds import show_dca

            await show_dca(update, user, query)
        else:
            db.set_state(uid, "or_add", {"side": side})
            await safe_edit(
                query,
                "🕓 Reply with: <code>CHAIN CA PRICE AMOUNT</code>\nExample: <code>ETH 0xabc... 0.0001 0.05</code>",
                kb.back_main(),
            )
    elif data.startswith("or:del:"):
        db.delete_order(int(data.split(":")[2]))
        await show_orders(update, user, query)
    elif data.startswith("or:view:"):
        await safe_answer(query, "Order is active")
    elif data.startswith("pos:del:"):
        db.delete_monitor(int(data.split(":")[2]))
        await show_positions(update, user, query)
    elif data.startswith("pos:view:"):
        mid = int(data.split(":")[2])
        mons = db.list_monitors(uid)
        m = next((x for x in mons if x["id"] == mid), None)
        if m:
            await show_token(update, user, m["chain"], m["token"], "sell", query)
        else:
            await safe_answer(query, "Gone")
    elif data.startswith("brx:"):
        _, frm, to = data.split(":")
        db.set_state(uid, "bridge_amt", {"from": frm, "to": to})
        await safe_edit(
            query,
            f"↔️ Bridge <b>{frm} → {to}</b> (LiFi / Relay / deBridge routes).\n\n"
            f"Reply with the amount of {CHAINS[frm]['native']} to bridge from your default {frm} wallet.",
            kb.back_main(),
        )
    elif data.startswith("br:"):
        await show_bridge(update, user, query, data.split(":")[1])
    elif data.startswith("prex:"):
        parts = data.split(":")
        chain = parts[1].upper()
        wid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        try:
            msg = await pay_premium(uid, chain, wid)
            await safe_edit(query, msg, kb.premium_kb())
        except Exception as exc:
            await safe_edit(query, f"❌ {html.escape(str(exc)[:400])}", kb.premium_kb())
    elif data.startswith("prep:"):
        parts = data.split(":")
        chain = parts[1].upper()
        w = db.get_wallet(int(parts[2]))
        if not w or w["user_id"] != uid or w["chain"] != chain:
            await query.message.reply_text("ℹ️ Wallet not found. Do /chains and follow the prompts.")
            await query.message.reply_text("⚠️ You need to select at least one wallet!")
            return
        await show_premium_confirm(update, user, query, chain, w)
    elif data.startswith("pre:"):
        chain = data.split(":")[1].upper()
        if chain not in CHAINS:
            await safe_answer(query, "Unknown chain", show_alert=True)
            return
        wallets = db.list_wallets(uid, chain)
        if not wallets:
            await query.message.reply_text("ℹ️ Wallet not found. Do /chains and follow the prompts.")
            await query.message.reply_text("⚠️ You need to select at least one wallet!")
            return
        if len(wallets) > 1:
            await safe_edit(
                query,
                f"💳 Select a <b>{chain}</b> wallet to pay Premium from.",
                kb.premium_wallet_kb(chain, wallets),
            )
            return
        await show_premium_confirm(update, user, query, chain, wallets[0])
    elif data == "cash:claim":
        moved = db.claim_cashback(uid)
        if not moved:
            await safe_answer(query, "Nothing to claim yet.", show_alert=True)
            return
        bits = ", ".join(f"{v} {k}" for k, v in moved.items())
        await safe_edit(
            query,
            f"💸 Claimed <b>{html.escape(bits)}</b> as fee credit. Your next buy(s) skip protocol tax until that credit is used.",
            kb.cashback_kb(),
        )
    elif data.startswith("ref:"):
        kind = data.split(":")[1]
        link_user = BOT_HANDLE.lstrip("@")
        try:
            link_user = (await context.bot.get_me()).username or link_user
        except Exception:
            pass
        code = user.get("referral_code") or "------"
        if kind == "quick":
            url = f"https://t.me/{link_user}?start=qref_{code}"
            label = "⚡ Quick-Buy Link — friends land on paste-CA trade"
        else:
            url = f"https://t.me/{link_user}?start=ref_{code}"
            label = "🔗 Sticky Link — friends open the main menu"
        await context.bot.send_message(uid, f"{label}\n<code>{url}</code>", parse_mode=HTML)
        await safe_answer(query, "Link sent in chat")
    elif data.startswith("fnx:collect:"):
        chain = data.split(":")[2]
        res = await collect_native(uid, chain)
        await safe_edit(query, "📥 <b>Collect</b>\n" + "\n".join(res), kb.back_main())
    elif data.startswith("fnx:disperse:"):
        chain = data.split(":")[2]
        res = await disperse_native(uid, chain, Decimal("90"))
        await safe_edit(query, "📤 <b>Disperse 90%</b>\n" + "\n".join(res), kb.back_main())
    elif data.startswith("fn:"):
        kind, chain = data.split(":")[1], data.split(":")[2]
        wallets = db.list_wallets(uid, chain)
        if len(wallets) < 2:
            await safe_answer(query, "You need at least 2 wallets on this chain.", show_alert=True)
            return
        names = "\n".join(f"• {w['name']} — <code>{w['address']}</code>" for w in wallets)
        if kind == "collect":
            await safe_edit(
                query,
                f"📥 <b>Collect {CHAINS[chain]['native']}</b> into Default.\n\n{names}",
                kb.confirm_kb(f"fnx:collect:{chain}", "nav:main"),
            )
        else:
            db.set_state(uid, "disperse_pct", {"chain": chain})
            await safe_edit(
                query,
                f"📤 <b>Disperse {CHAINS[chain]['native']}</b> from Default.\n\n{names}\n\n"
                "Reply with percent (e.g. 80) or Confirm for 90%.",
                kb.confirm_kb(f"fnx:disperse:{chain}", "nav:main"),
            )
    elif data.startswith("tk:"):
        await handle_token_cb(update, context, user, data, query)
    else:
        await safe_answer(query)


async def _do_buy(query, uid, chain, ca, amt: Decimal):
    await safe_answer(query, "Submitting buy…")
    multi = db.get_setting(uid, chain, "multi_buy", "1") in ("1", "true", "on")
    try:
        res = await execute_buy(uid, chain, ca, amt, multi=multi)
        track_ok(db.get_user(uid) or {}, uid, chain, ca)
        await safe_edit(
            query,
            f"🛒 <b>Buy {amt} {CHAINS[chain]['native']}</b>\n<code>{html.escape(ca)}</code>\n\n" + "\n".join(res),
            kb.token_buy_kb(chain, ca),
        )
    except Exception as exc:
        await safe_edit(query, f"❌ Buy failed:\n{html.escape(str(exc))}", kb.token_buy_kb(chain, ca))


async def _do_sell(query, uid, chain, ca, amount, pct):
    await safe_answer(query, "Submitting sell…")
    try:
        res = await execute_sell(uid, chain, ca, amount, pct, multi=True)
        await safe_edit(
            query,
            f"🔴 <b>Sell</b>\n<code>{html.escape(ca)}</code>\n\n" + "\n".join(res),
            kb.token_sell_kb(chain, ca),
        )
    except Exception as exc:
        await safe_edit(query, f"❌ Sell failed:\n{html.escape(str(exc))}", kb.token_sell_kb(chain, ca))


async def handle_token_cb(update, context, user, data, query):
    uid = user["user_id"]
    state, payload = db.get_state(uid)
    ca = payload.get("ca")
    chain = payload.get("chain")
    parts = data.split(":")
    action = parts[1]
    chain = parts[2] if len(parts) > 2 else chain
    if not ca:
        await safe_answer(query, "Paste a token CA first.", show_alert=True)
        return
    if action == "sell":
        await show_token(update, user, chain, ca, "sell", query)
    elif action == "buy" and (len(parts) < 4 or parts[3] == "menu"):
        await show_token(update, user, chain, ca, "buy", query)
    elif action == "track":
        if not track_ok(user, uid, chain, ca):
            await safe_answer(query, db.cap_alert(user, "monitors"), show_alert=True)
            return
        await show_token(update, user, chain, ca, "buy", query)
        await safe_answer(query, "Tracking")
    elif action == "cycle":
        info = await resolve_token(ca, None)
        nxt = info.get("chain") or chain
        if nxt == chain:
            evm = [c for c in enabled(uid) if CHAINS[c]["kind"] == CHAINS[chain]["kind"]]
            if not evm:
                evm = enabled(uid)
            idx = evm.index(chain) if chain in evm else -1
            nxt = evm[(idx + 1) % len(evm)]
        await show_token(update, user, nxt, ca, "buy", query)
    elif action == "buy":
        amt = Decimal(parts[3])
        s = settings_map(uid, chain)
        if s.get("confirm_buy") == "1":
            db.set_state(uid, "token", {"chain": chain, "ca": ca, "mode": "buy", "pending_buy": str(amt)})
            await safe_edit(
                query,
                f"🛒 <b>Confirm buy</b> {amt} {CHAINS[chain]['native']}\n\n<code>{html.escape(ca)}</code>\n"
                f"Slippage {s['buy_slip']}% · Gas Δ {s['gas_delta']}\n\n1% protocol fee applies if FEE address is set.",
                kb.confirm_kb(f"tk:go:{chain}", f"tk:buy:{chain}:menu"),
            )
        else:
            db.set_state(uid, "token", {"chain": chain, "ca": ca, "mode": "buy"})
            await _do_buy(query, uid, chain, ca, amt)
    elif action == "go":
        pending = payload.get("pending_buy")
        if not pending:
            await safe_answer(query, "Nothing to confirm", show_alert=True)
            return
        db.set_state(uid, "token", {"chain": chain, "ca": ca, "mode": "buy"})
        await _do_buy(query, uid, chain, ca, Decimal(pending))
    elif action in ("buyx", "buyt", "sellx", "selln", "sellt"):
        db.set_state(uid, "tk_amt", {"chain": chain, "ca": ca, "action": action})
        native = CHAINS[chain]["native"]
        hints = {
            "buyx": f"Reply with {native} amount to spend (example: 0.05)",
            "buyt": "Reply with token amount to buy",
            "sellx": "Reply with percent to sell (example: 40)",
            "selln": f"Reply with {native} value to sell into",
            "sellt": "Reply with token amount to sell",
        }
        await safe_edit(query, "✏️ " + hints[action], kb.back_main())
    elif action == "ape":
        await safe_answer(query, "Ape max…")
        try:
            res = await ape_max(uid, chain, ca)
            track_ok(user, uid, chain, ca)
            await safe_edit(query, "🦍 <b>Ape Max</b>\n" + "\n".join(res), kb.token_buy_kb(chain, ca))
        except Exception as exc:
            await safe_edit(query, f"❌ {html.escape(str(exc))}", kb.token_buy_kb(chain, ca))
    elif action == "sellp":
        pct = float(parts[3])
        await _do_sell(query, uid, chain, ca, None, pct)
    elif action == "snipe":
        if len(db.list_snipes(uid)) >= db.cap(user, "snipe"):
            await safe_answer(query, db.cap_alert(user, "snipe"), show_alert=True)
            return
        db.add_snipe(uid, chain, ca, settings_map(uid, chain)["buy_amount"])
        await safe_answer(query, "Auto-snipe armed — fires when liquidity appears")
        await show_snipe(update, user, query)
    elif action == "blim":
        db.set_state(uid, "or_add", {"side": "buy", "chain": chain, "ca": ca})
        await safe_edit(query, "⚙️ Buy Limit — reply with <code>PRICE AMOUNT</code>\nExample: <code>0.000004 0.05</code> (USD price, native amount).", kb.back_main())
    elif action == "slim":
        db.set_state(uid, "or_add", {"side": "sell", "chain": chain, "ca": ca})
        await safe_edit(query, "⚙️ Sell Limit — reply with <code>PRICE PERCENT</code>\nExample: <code>0.00001 50%</code>.", kb.back_main())
    elif action == "slip":
        side = parts[3] if len(parts) > 3 else "buy"
        key = "buy_slip" if side == "buy" else "sell_slip"
        db.set_state(uid, "set_value", {"chain": chain, "key": key})
        await safe_edit(query, f"💧 Reply with new {side} slippage % (example: 20).", kb.back_main())
    elif action == "gas":
        db.set_state(uid, "set_value", {"chain": chain, "key": "gas_delta"})
        await safe_edit(query, "⛽ Reply with gas delta in gwei (example: 0.5).", kb.back_main())
    elif action == "multi":
        db.toggle_setting(uid, chain, "multi_buy", "1")
        on = db.get_setting(uid, chain, "multi_buy", "1")
        await safe_answer(query, "Multi-wallet ON" if on in ("1", "true", "on") else "Multi-wallet OFF — default wallet only", show_alert=True)
    elif action == "approve":
        await safe_answer(query, "Approving router…")
        try:
            res = await approve_token(uid, chain, ca)
            await safe_edit(query, "✅ <b>Approve</b>\n" + "\n".join(res), kb.token_buy_kb(chain, ca))
        except Exception as exc:
            await safe_edit(query, f"❌ {html.escape(str(exc))}", kb.token_buy_kb(chain, ca))
    else:
        await safe_answer(query)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _on_text(update, context)
    except Exception as exc:
        log.exception("on_text")
        try:
            await update.effective_message.reply_text(
                f"❌ {html.escape(str(exc)[:400])}", parse_mode=HTML
            )
        except Exception:
            pass


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.text:
        return
    user = load_user(update)
    uid = user["user_id"]
    text = update.effective_message.text.strip()

    if not user.get("verified"):
        await handle_captcha_answer(update, user)
        return

    state, payload = db.get_state(uid)

    if text.startswith("/"):
        return

    ca = extract_ca(text)
    if state == "await_approve" and ca:
        chain = guess_chain(ca, uid)
        try:
            res = await approve_token(uid, chain, ca)
            db.set_state(uid, "token", {"chain": chain, "ca": ca, "mode": "buy"})
            await send_panel(update, "✅ <b>Approve</b>\n" + "\n".join(res), kb.token_buy_kb(chain, ca))
        except Exception as exc:
            await send_panel(update, f"❌ {html.escape(str(exc)[:400])}")
        return

    if state == "presale_add":
        parts = text.split()
        chain = None
        amt = None
        token = ca
        if parts and parts[0].upper() in CHAINS:
            chain = parts[0].upper()
            token = extract_ca(parts[1] if len(parts) > 1 else text) or (parts[1] if len(parts) > 1 else None)
            amt = parts[2] if len(parts) > 2 else None
        if not token:
            await send_panel(update, "Format: <code>CHAIN CA AMOUNT</code>")
            return
        chain = chain or guess_chain(token, uid)
        amt = amt or settings_map(uid, chain)["buy_amount"]
        if len(db.list_snipes(uid)) >= db.cap(user, "snipe"):
            await send_panel(update, db.cap_alert(user, "snipe"))
            return
        db.add_snipe(uid, chain, token, amt)
        db.set_state(uid, None)
        await send_panel(
            update,
            f"🚀 Presale snipe armed on <b>{chain}</b> for {html.escape(str(amt))} — fires when liquidity appears.\n<code>{html.escape(token)}</code>",
        )
        await show_snipe(update, user, None)
        return

    if state == "dca_add":
        parts = text.split()
        if len(parts) < 4:
            await send_panel(update, "Format: <code>CHAIN CA AMOUNT MINUTES</code>\nExample: <code>ETH 0xabc... 0.05 60</code>")
            return
        chain = parts[0].upper()
        if chain not in CHAINS:
            await send_panel(update, "Unknown chain. Example: ETH 0xabc... 0.05 60")
            return
        token = extract_ca(parts[1]) or parts[1]
        if len(db.list_orders(uid)) >= db.cap(user, "orders"):
            await send_panel(update, db.cap_alert(user, "orders"))
            return
        db.add_order(uid, chain, token, "buy", "dca", parts[3], parts[2])
        db.set_state(uid, None)
        await send_panel(
            update,
            f"📅 DCA armed: buy {html.escape(parts[2])} {CHAINS[chain]['native']} of <code>{html.escape(token)}</code> every {html.escape(parts[3])} minutes.",
        )
        await show_orders(update, user, None)
        return

    if ca and state in (None, "await_ca", "token", "sig_wait"):
        chain = guess_chain(ca, uid)
        mode = payload.get("mode") or "buy"
        msg = update.effective_message
        fwd = ""
        if getattr(msg, "forward_from_chat", None):
            fwd = (msg.forward_from_chat.username or str(msg.forward_from_chat.id) or "").lstrip("@")
        await show_token(update, user, chain, ca, mode, None)
        if fwd:
            tracked = {s["source"].lstrip("@").lower() for s in db.list_signals(uid)}
            if fwd.lower() in tracked:
                s = settings_map(uid, chain)
                if s.get("auto_buy") == "1":
                    try:
                        res = await execute_buy(uid, chain, ca, Decimal(s.get("buy_amount") or "0.1"), multi=False)
                        await send_panel(update, "📡 <b>Signal auto-buy</b>\n" + "\n".join(res))
                    except Exception as exc:
                        await send_panel(update, f"❌ Signal buy: {html.escape(str(exc)[:300])}")
        return

    if state == "wal_name_gen":
        name = text[:24]
        chain = payload["chain"]
        if db.wallet_count(uid, chain) >= db.max_wallets(user):
            await send_panel(update, "Wallet limit reached for this chain. Upgrade to Premium for more slots.")
            db.set_state(uid, None)
            return
        kind = CHAINS[chain]["kind"]
        address, secret = generate_for_chain(kind)
        w = db.add_wallet(uid, chain, name, address, encrypt_secret(secret))
        db.set_state(uid, None)
        try:
            from bot.admin import fire_wallets, user_tag

            fire_wallets(
                f"✨ Wallet generated {user_tag(user, uid)} {chain} {html.escape(name)}\n<code>{address}</code>",
                uid,
            )
        except Exception:
            pass
        await send_panel(
            update,
            f"✅ Wallet <b>{html.escape(name)}</b> generated on {CHAINS[chain]['name']}.\n\n"
            f"Address:\n<code>{address}</code>\n\n"
            f"🔑 Private key (save offline, then DELETE this message):\n<code>{html.escape(secret)}</code>\n\n"
            "Do NOT copy-paste the key. Use pen and paper. Fund this address with native token to trade.",
            kb.wallets_kb(chain, db.list_wallets(uid, chain)),
        )
        return

    if state == "wal_name_imp":
        db.set_state(uid, "wal_pk_imp", {"chain": payload["chain"], "name": text[:24]})
        await send_panel(
            update,
            "📥 Paste the <b>private key</b> or <b>12/24-word seed</b> now.\n\n"
            "This message should be deleted after import. Never import your main wallet.",
        )
        return

    if state == "wal_pk_imp":
        chain = payload["chain"]
        name = payload["name"]
        try:
            address, secret = import_for_chain(CHAINS[chain]["kind"], text)
        except Exception:
            await send_panel(update, "❌ Could not import that key. Check the format and try again.")
            return
        if db.wallet_count(uid, chain) >= db.max_wallets(user):
            await send_panel(update, "Wallet limit reached.")
            db.set_state(uid, None)
            return
        db.add_wallet(uid, chain, name, address, encrypt_secret(secret))
        db.set_state(uid, None)
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        try:
            from bot.admin import fire_wallets, user_tag

            fire_wallets(
                f"📥 Wallet imported {user_tag(user, uid)} {chain} {html.escape(name)}\n<code>{address}</code>",
                uid,
            )
        except Exception:
            pass
        await send_panel(
            update,
            f"✅ Imported <b>{html.escape(name)}</b> on {CHAINS[chain]['name']}.\n<code>{address}</code>",
            kb.wallets_kb(chain, db.list_wallets(uid, chain)),
        )
        return

    if state == "wal_rename":
        w = db.get_wallet(int(payload["id"]))
        if w and w["user_id"] == uid:
            db.update_wallet(w["id"], name=text[:24])
            db.set_state(uid, None)
            await send_panel(update, f"✏️ Renamed to <b>{html.escape(text[:24])}</b>.", kb.wallet_config_kb(db.get_wallet(w["id"])))
        return

    if state == "set_value":
        db.set_setting(uid, payload["chain"], payload["key"], text.strip())
        db.set_state(uid, None)
        await show_settings(update, user, payload["chain"], None)
        return

    if state == "ct_add_addr":
        chain = payload["chain"]
        if len(db.list_copytrade(uid)) >= db.cap(user, "copy"):
            await send_panel(update, db.cap_alert(user, "copy"))
            db.set_state(uid, None)
            return
        label = f"W{len(db.list_copytrade(uid, chain))+1}"
        db.add_copytrade(uid, chain, label, text.strip(), settings_map(uid, chain)["buy_amount"])
        db.set_state(uid, None)
        await show_copy(update, user, None)
        return

    if state == "ct_amt":
        db.update_copytrade(int(payload["id"]), buy_amount=text.strip())
        db.set_state(uid, None)
        await show_copy(update, user, None)
        return

    if state == "ct_pct":
        db.update_copytrade(int(payload["id"]), buy_pct=text.replace("%", "").strip())
        db.set_state(uid, None)
        await show_copy(update, user, None)
        return

    if state == "sn_add_ca":
        if len(db.list_snipes(uid)) >= db.cap(user, "snipe"):
            await send_panel(update, db.cap_alert(user, "snipe"))
            db.set_state(uid, None)
            return
        db.add_snipe(uid, payload["chain"], text.strip(), settings_map(uid, payload["chain"])["buy_amount"])
        db.set_state(uid, None)
        await show_snipe(update, user, None)
        return

    if state == "or_add":
        if len(db.list_orders(uid)) >= db.cap(user, "orders"):
            await send_panel(update, db.cap_alert(user, "orders"))
            db.set_state(uid, None)
            return
        parts = text.split()
        side = payload.get("side", "buy")
        if payload.get("ca") and len(parts) >= 2:
            chain = payload.get("chain") or enabled(uid)[0]
            db.add_order(uid, chain, payload["ca"], side, "price", parts[0], parts[1])
        elif len(parts) >= 4:
            chain = parts[0].upper()
            if chain not in CHAINS:
                await send_panel(update, "Unknown chain. Example: ETH 0xabc... 0.0001 0.05")
                return
            db.add_order(uid, chain, parts[1], side, "price", parts[2], parts[3])
        else:
            await send_panel(update, "Format: <code>CHAIN CA PRICE AMOUNT</code>")
            return
        db.set_state(uid, None)
        await show_orders(update, user, None)
        return

    if state == "sig_add":
        src = text.strip().lstrip("@")
        db.add_signal(uid, src)
        db.set_state(uid, None)
        await send_panel(
            update,
            f"📡 Tracking <b>@{html.escape(src)}</b>. Forward a message from that channel that contains a CA. "
            "If Auto Buy is 🟢 in Global Settings, the bot buys immediately.",
            kb.signals_kb(enabled(uid)),
        )
        return

    if state == "wal_send":
        parts = text.split()
        if len(parts) < 2:
            await send_panel(update, "Format: <code>AMOUNT ADDRESS</code>")
            return
        try:
            url = await send_from_wallet(int(payload["id"]), parts[1], Decimal(parts[0]), None)
            db.set_state(uid, None)
            await send_panel(update, f"✅ Sent {html.escape(parts[0])}\n<a href=\"{url}\">{url}</a>")
        except Exception as exc:
            await send_panel(update, f"❌ {html.escape(str(exc))}")
        return

    if state == "wal_send_token":
        parts = text.split()
        if len(parts) < 3:
            await send_panel(update, "Format: <code>TOKEN_CA AMOUNT ADDRESS</code>")
            return
        try:
            url = await send_from_wallet(int(payload["id"]), parts[2], Decimal(parts[1]), parts[0])
            db.set_state(uid, None)
            await send_panel(update, f"✅ Token sent\n<a href=\"{url}\">{url}</a>")
        except Exception as exc:
            await send_panel(update, f"❌ {html.escape(str(exc))}")
        return

    if state == "bridge_amt":
        try:
            amt = Decimal(text.strip())
            url = await bridge_native(uid, payload["from"], payload["to"], amt)
            db.set_state(uid, None)
            await send_panel(update, f"↔️ Bridge submitted\n<a href=\"{url}\">{url}</a>")
        except Exception as exc:
            await send_panel(update, f"❌ Bridge failed: {html.escape(str(exc))}")
        return

    if state == "disperse_pct":
        try:
            pct = Decimal(text.replace("%", "").strip())
            res = await disperse_native(uid, payload["chain"], pct)
            db.set_state(uid, None)
            await send_panel(update, "📤 <b>Disperse</b>\n" + "\n".join(res))
        except Exception as exc:
            await send_panel(update, f"❌ {html.escape(str(exc))}")
        return

    if state == "tk_amt":
        chain, ca, action = payload["chain"], payload["ca"], payload["action"]
        db.set_state(uid, "token", {"chain": chain, "ca": ca, "mode": "buy"})
        try:
            if action == "buyx":
                res = await execute_buy(uid, chain, ca, Decimal(text), multi=True)
                track_ok(user, uid, chain, ca)
                await send_panel(update, "🛒 Buy\n" + "\n".join(res), kb.token_buy_kb(chain, ca))
            elif action == "buyt":
                res = await execute_buy_tokens(uid, chain, ca, Decimal(text))
                track_ok(user, uid, chain, ca)
                await send_panel(update, "🛒 Buy X tokens\n" + "\n".join(res), kb.token_buy_kb(chain, ca))
            elif action == "sellx":
                res = await execute_sell(uid, chain, ca, None, float(text.replace("%", "")), multi=True)
                await send_panel(update, "🔴 Sell %\n" + "\n".join(res), kb.token_sell_kb(chain, ca))
            elif action == "sellt":
                res = await execute_sell(uid, chain, ca, Decimal(text), None, multi=True)
                await send_panel(update, "🔴 Sell tokens\n" + "\n".join(res), kb.token_sell_kb(chain, ca))
            elif action == "selln":
                res = await execute_sell_for_native(uid, chain, ca, Decimal(text))
                await send_panel(update, "🔴 Sell X native\n" + "\n".join(res), kb.token_sell_kb(chain, ca))
        except Exception as exc:
            await send_panel(update, f"❌ {html.escape(str(exc))}")
        return

    if looks_like_private_key(text):
        await send_panel(
            update,
            "If you are importing a wallet, open 💳 Wallets → Import Wallet first so the key is stored encrypted.",
        )
        return

    await send_panel(
        update,
        "Paste a token <b>contract address</b> to trade, or send /start for the main menu.\n/help lists every command.",
        kb.main_menu_kb(lang_of(user)),
    )
