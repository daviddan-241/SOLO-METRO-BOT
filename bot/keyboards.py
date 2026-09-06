from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from bot.config import (
    BOT_NAME,
    CHAINS,
    CHAIN_ORDER,
    SUPPORT_URL,
)


def lp() -> LinkPreviewOptions:
    return LinkPreviewOptions(is_disabled=True)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _nav(back: str | None = "nav:main", back_label: str = "⬅️ Back") -> list[list]:
    if back and back != "nav:main":
        return [[_btn(back_label, back), _btn("❌ Close", "nav:main")]]
    return [[_btn("⬅️ Back", "nav:main"), _btn("❌ Close", "nav:main")]]


def main_menu_kb(lang: str = "en") -> InlineKeyboardMarkup:
    lang_label = "🇺🇸 🇨🇳 Language" if lang == "en" else "🇺🇸 🇨🇳 语言"
    cash = "💸 Cashback" if lang == "en" else "💸 返佣"
    rows = [
        [_btn("🔗 Chains", "nav:chains"), _btn(lang_label, "nav:lang")],
        [_btn("💳 Wallets", "nav:wallets"), _btn("⚙️ Global Settings", "nav:settings")],
        [_btn("📡 Signals", "nav:signals"), _btn("👫 Copytrade", "nav:copy")],
        [_btn("🕐 Active Orders", "nav:orders"), _btn("📈 Positions", "nav:pos")],
        [_btn("🎯 Auto Snipe", "nav:snipe"), _btn("↔️ Bridge", "nav:bridge")],
        [_btn("⭐ Premium", "nav:premium"), _btn(cash, "nav:cash"), _btn("💰 Referral", "nav:ref")],
        [_btn("⚡ BUY & SELL NOW!", "nav:buysell")],
    ]
    return InlineKeyboardMarkup(rows)


def authorized_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("🇨🇳", "nav:lang:zh")]])


def onboard_kb(lang: str = "en") -> InlineKeyboardMarkup:
    """First post-captcha welcome: tap Continue → main menu + wallet prompt."""
    lang_btn = "🇨🇳" if lang != "zh" else "🇺🇸"
    lang_data = "nav:lang:zh" if lang != "zh" else "nav:lang:en"
    return InlineKeyboardMarkup(
        [
            [_btn("▶️ Continue", "nav:onboard")],
            [_btn(lang_btn, lang_data)],
        ]
    )


def back_main(extra: list | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def chains_kb(flags: dict[str, int]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for chain in CHAIN_ORDER:
        on = flags.get(chain, 1)
        mark = "✅" if on else "❌"
        label = f"{mark} {CHAINS[chain]['emoji']} {chain}"
        row.append(_btn(label, f"ch:tog:{chain}"))
        row.append(_btn("💳", f"wal:list:{chain}"))
        if len(row) >= 2:
            # two pairs per row would be 4 buttons; keep one pair per row for readability
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("🔄 Enable All", "ch:all:1"), _btn("🚫 Disable All", "ch:all:0")])
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def need_wallet_kb(chain: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("✨ Generate Wallet", f"wal:gen:{chain}"), _btn("📥 Import Wallet", f"wal:imp:{chain}")],
            [_btn("♻️ Auto-Generate W1", f"wal:auto:{chain}")],
            [_btn("💳 All chains", "nav:wallets"), _btn("❌ Close", "nav:main")],
        ]
    )


def wallets_chain_pick_kb(enabled: list[str]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for chain in enabled:
        row.append(_btn(f"{CHAINS[chain]['emoji']} {chain}", f"wal:list:{chain}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def wallets_kb(chain: str, wallets: list) -> InlineKeyboardMarkup:
    rows = []
    for w in wallets:
        star = "⭐" if w["is_default"] else "💳"
        man = "🟢" if w["is_manual"] else "🔴"
        rows.append(
            [
                _btn(f"{star} {w['name']}", f"wal:cfg:{w['id']}"),
                _btn(f"{man} Manual", f"wal:man:{w['id']}"),
            ]
        )
    rows.append(
        [
            _btn("✨ Generate Wallet", f"wal:gen:{chain}"),
            _btn("📥 Import Wallet", f"wal:imp:{chain}"),
        ]
    )
    rows.append([_btn("♻️ Auto-Generate W1", f"wal:auto:{chain}")])
    if wallets:
        rows.append([_btn("💳 Default Wallet", f"wal:def:{chain}")])
        rows.append([_btn("🗄 Rearrange Wallets", f"wal:arr:{chain}")])
    rows.append([_btn("⬅️ Chains", "nav:chains"), _btn("❌ Close", "nav:main")])
    return InlineKeyboardMarkup(rows)


def wallet_config_kb(w: dict) -> InlineKeyboardMarkup:
    native = CHAINS[w["chain"]]["native"]
    rows = [
        [_btn(f"⬆️ Send {native}", f"wal:sendn:{w['id']}"), _btn("⬆️ Send Tokens", f"wal:sendt:{w['id']}")],
        [_btn("📋 Copy Address", f"wal:addr:{w['id']}"), _btn("🔑 Export Key", f"wal:exp:{w['id']}")],
        [_btn("✏️ Rename", f"wal:ren:{w['id']}"), _btn("📥 Import Cross-Chain", f"wal:x:{w['id']}")],
        [_btn("♻️ Regenerate New Wallet", f"wal:regen:{w['chain']}")],
        [_btn("🗑 Disconnect", f"wal:del:{w['id']}")],
        [_btn("⬅️ Wallets", f"wal:list:{w['chain']}"), _btn("❌ Close", "nav:main")],
    ]
    return InlineKeyboardMarkup(rows)


def default_wallet_kb(chain: str, wallets: list) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{'✅ ' if w['is_default'] else ''}{w['name']}", f"wal:setdef:{w['id']}")] for w in wallets]
    rows.append([_btn("⬅️ Back", f"wal:list:{chain}")])
    return InlineKeyboardMarkup(rows)


def confirm_kb(yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("✅ Yes", yes), _btn("❌ No", no)],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def premium_confirm_kb(chain: str, wid: int) -> InlineKeyboardMarkup:
    url = (SUPPORT_URL or "").strip()
    rows = [
        [_btn("✅ Yes", f"prex:{chain}:{wid}"), _btn("❌ No", "nav:premium")],
    ]
    if url.startswith("http"):
        rows.append([InlineKeyboardButton(f"⭐ {BOT_NAME} Pro Bot ⭐", url=url)])
    rows.append([_btn("❌ Close", "nav:main")])
    return InlineKeyboardMarkup(rows)


def premium_wallet_kb(chain: str, wallets: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for w in wallets:
        star = "⭐ " if w.get("is_default") else ""
        row.append(_btn(f"{star}{w['name']}", f"prep:{chain}:{w['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("❌ Close", "nav:main")])
    return InlineKeyboardMarkup(rows)


def settings_chain_pick_kb(enabled: list[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for chain in enabled:
        row.append(_btn(f"{CHAINS[chain]['emoji']} {chain}", f"set:view:{chain}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def settings_kb(chain: str) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🟢/🔴 Anti-MEV", f"set:tog:{chain}:anti_mev"), _btn("🟢/🔴 Degen Mode", f"set:tog:{chain}:degen")],
        [_btn("🟢/🔴 Anti-Rug", f"set:tog:{chain}:anti_rug"), _btn("🟢/🔴 Smart Slippage", f"set:tog:{chain}:smart_slip")],
        [_btn("🟢/🔴 Auto Buy", f"set:tog:{chain}:auto_buy"), _btn("🟢/🔴 Auto-Approve", f"set:tog:{chain}:auto_approve")],
        [_btn("🟢/🔴 Confirm Manual Buy", f"set:tog:{chain}:confirm_buy")],
        [_btn("💧 Buy Slippage", f"set:ask:{chain}:buy_slip"), _btn("💧 Sell Slippage", f"set:ask:{chain}:sell_slip")],
        [_btn("⛽ Gas Delta", f"set:ask:{chain}:gas_delta"), _btn("⛽ Max Gas", f"set:ask:{chain}:max_gas")],
        [_btn("💰 Buy Amount", f"set:ask:{chain}:buy_amount")],
        [_btn("🛒 Buy Settings", f"set:buy:{chain}"), _btn("💸 Sell Settings", f"set:sell:{chain}")],
        [_btn("⬅️ Chains", "nav:settings"), _btn("❌ Close", "nav:main")],
    ]
    return InlineKeyboardMarkup(rows)


def signals_kb(enabled: list[str]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"📡 {CHAINS[c]['emoji']} {c}", f"sig:ch:{c}")] for c in enabled]
    rows.append([_btn("➕ Add Signal Channel", "sig:add")])
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def copytrade_kb(enabled: list[str], items: list) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        mark = "🟢" if it["enabled"] else "🔴"
        rows.append(
            [
                _btn(f"{mark} {it['chain']} {it['label']}", f"ct:view:{it['id']}"),
                _btn("🗑", f"ct:del:{it['id']}"),
            ]
        )
    rows.append([_btn("➕ Add Wallet to Copy", "ct:add")])
    row = []
    for c in enabled[:6]:
        row.append(_btn(c, f"ct:ch:{c}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def copytrade_item_kb(item: dict) -> InlineKeyboardMarkup:
    mark = "🟢 Enabled" if item["enabled"] else "🔴 Disabled"
    sell = "🟢 Copy Sell" if item["copy_sell"] else "🔴 Copy Sell"
    return InlineKeyboardMarkup(
        [
            [_btn(mark, f"ct:tog:{item['id']}"), _btn(sell, f"ct:sell:{item['id']}")],
            [_btn("💰 Max Buy", f"ct:amt:{item['id']}"), _btn("📊 Buy %", f"ct:pct:{item['id']}")],
            [_btn("⬅️ Copytrade", "nav:copy"), _btn("❌ Close", "nav:main")],
        ]
    )


def snipe_kb(items: list, enabled: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        mark = "🟢" if it["enabled"] else "🔴"
        short = it["token"][:8] + "…" if len(it["token"]) > 10 else it["token"]
        rows.append(
            [
                _btn(f"{mark} {it['chain']} {short}", f"sn:view:{it['id']}"),
                _btn("🗑", f"sn:del:{it['id']}"),
            ]
        )
    rows.append([_btn("➕ New Auto Snipe", "sn:add")])
    row = []
    for c in enabled:
        if CHAINS[c].get("autosnipe"):
            row.append(_btn(c, f"sn:ch:{c}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def orders_kb(orders: list) -> InlineKeyboardMarkup:
    rows = []
    for o in orders:
        short = o["token"][:8] + "…" if len(o["token"]) > 10 else o["token"]
        rows.append(
            [
                _btn(f"{o['side'].upper()} {o['chain']} {short}", f"or:view:{o['id']}"),
                _btn("🗑", f"or:del:{o['id']}"),
            ]
        )
    rows.append([_btn("➕ Add Buy Limit", "or:add:buy"), _btn("➕ Add Sell Limit", "or:add:sell")])
    rows.append([_btn("📅 Add DCA", "or:add:dca")])
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def positions_kb(monitors: list) -> InlineKeyboardMarkup:
    rows = []
    for m in monitors:
        short = m["token"][:10] + "…" if len(m["token"]) > 12 else m["token"]
        rows.append(
            [
                _btn(f"{m['chain']} {short}", f"pos:view:{m['id']}"),
                _btn("🗑", f"pos:del:{m['id']}"),
            ]
        )
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def bridge_kb(enabled: list[str] | None = None) -> InlineKeyboardMarkup:
    rows = [
        [_btn("⚡ Relay", "br:relay"), _btn("🌉 deBridge", "br:debridge")],
        [_btn("🕵️ HoudiniSwap (Private)", "br:private"), _btn("🟦 Arc → USDC", "br:arc")],
    ]
    pairs = [
        ("ETH", "SOL"),
        ("SOL", "ETH"),
        ("ETH", "BSC"),
        ("BSC", "ETH"),
        ("ETH", "BASE"),
        ("BASE", "ETH"),
        ("ETH", "ARB"),
        ("ARB", "ETH"),
        ("SOL", "BSC"),
        ("BSC", "SOL"),
    ]
    enabled = set(enabled or CHAIN_ORDER)
    row = []
    for a, b in pairs:
        if a in enabled and b in enabled:
            row.append(_btn(f"{a} → {b}", f"brx:{a}:{b}"))
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def premium_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("Pay in $SOL (SOL)", "pre:SOL"), _btn("Pay in $BNB (BSC)", "pre:BSC")],
            [_btn("Pay in $ETH (BASE)", "pre:BASE"), _btn("Pay in $ETH (ETH)", "pre:ETH")],
            [_btn("Pay in $MON (MONAD)", "pre:MONAD"), _btn("Pay in $S (SONIC)", "pre:SONIC")],
            [_btn("Pay in $ETH (ARB)", "pre:ARB")],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def extra_hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("🎯 Auto Snipe", "nav:snipe"), _btn("⚡ God Mode", "nav:god")],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def campaigns_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("⭐ Premium", "nav:premium"), _btn("💸 Cashback", "nav:cash")],
            [_btn("🎃 PumpFun", "nav:pump"), _btn("💰 Referral", "nav:ref")],
            [_btn("🏆 Competition", "nav:comp")],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def cashback_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("💸 Claim Cashback", "cash:claim"), _btn("🎃 PumpFun Panel", "nav:pump")],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def referral_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("🔗 Sticky Link", "ref:sticky"), _btn("⚡ Quick-Buy Link", "ref:quick")],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def token_buy_kb(chain: str, ca: str) -> InlineKeyboardMarkup:
    native = CHAINS[chain]["native"]
    ape = CHAINS[chain].get("ape_max")
    rows = [
        [_btn("📍 Track", f"tk:track:{chain}"), _btn(f"🔄 {chain}", f"tk:cycle:{chain}")],
        [_btn("✅ Approve", f"tk:approve:{chain}"), _btn("💳 Multi", f"tk:multi:{chain}")],
        [_btn("⇔ Go to Sell", f"tk:sell:{chain}")],
        [_btn(f"Buy 0.01 {native}", f"tk:buy:{chain}:0.01"), _btn(f"Buy 0.05 {native}", f"tk:buy:{chain}:0.05")],
        [_btn(f"Buy 0.1 {native}", f"tk:buy:{chain}:0.1"), _btn(f"Buy X {native}", f"tk:buyx:{chain}")],
        [_btn("Buy X Tokens", f"tk:buyt:{chain}")] + ([_btn("🦍 Ape Max", f"tk:ape:{chain}")] if ape else []),
        [_btn("💧 Slippage", f"tk:slip:{chain}:buy"), _btn("⛽ Gas", f"tk:gas:{chain}:buy")],
        [_btn("🎯 Snipe", f"tk:snipe:{chain}"), _btn("⚙️ Buy Limit", f"tk:blim:{chain}")],
    ]
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def token_sell_kb(chain: str, ca: str) -> InlineKeyboardMarkup:
    native = CHAINS[chain]["native"]
    rows = [
        [_btn("📍 Track", f"tk:track:{chain}"), _btn(f"🔄 {chain}", f"tk:cycle:{chain}")],
        [_btn("💳 Multi", f"tk:multi:{chain}"), _btn("⇔ Go to Buy", f"tk:buy:{chain}:menu")],
        [_btn("☢️ Sell All", f"tk:sellp:{chain}:100"), _btn("Sell 50%", f"tk:sellp:{chain}:50")],
        [_btn("Sell 25%", f"tk:sellp:{chain}:25"), _btn("Sell X%", f"tk:sellx:{chain}")],
        [_btn(f"Sell X {native}", f"tk:selln:{chain}"), _btn("Sell X Tokens", f"tk:sellt:{chain}")],
        [_btn("💧 Slippage", f"tk:slip:{chain}:sell"), _btn("⛽ Gas", f"tk:gas:{chain}:sell")],
        [_btn("⚙️ Sell Limit", f"tk:slim:{chain}")],
    ]
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def funds_kb(kind: str, enabled: list[str]) -> InlineKeyboardMarkup:
    title_map = {"collect": "Collect", "disperse": "Disperse"}
    rows, row = [], []
    for c in enabled:
        row.append(_btn(f"{CHAINS[c]['emoji']} {c}", f"fn:{kind}:{c}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(_nav())
    return InlineKeyboardMarkup(rows)


def language_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn(("✅ " if lang == "en" else "") + "🇺🇸 English", "nav:lang:en")],
            [_btn(("✅ " if lang == "zh" else "") + "🇨🇳 中文", "nav:lang:zh")],
            [_btn("❌ Close", "nav:main")],
        ]
    )


def monitor_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("📊 Open Positions", "nav:pos"), _btn("📋 Summary", "nav:summary")],
            [_btn("🧹 Clear Tracked Tokens", "nav:clear")],
            [_btn("❌ Close", "nav:main")],
        ]
    )
