import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Solo Metro").strip() or "Solo Metro"
BOT_HANDLE = os.getenv("BOT_HANDLE", "@SoloMetroBot").strip()
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@SoloMetroSupport").strip()

HUB_URL = os.getenv("HUB_URL", "https://t.me/SoloMetroHub")
UPDATES_URL = os.getenv("UPDATES_URL", "https://t.me/SoloMetroUpdates")
TWITTER_URL = os.getenv("TWITTER_URL", "https://x.com/SoloMetroBot")
DOCS_URL = os.getenv("DOCS_URL", "https://docs.solometro.bot")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/SoloMetroSupport")
TOS_URL = os.getenv("TOS_URL", "https://docs.solometro.bot/tos")
MORE_LINKS_URL = os.getenv("MORE_LINKS_URL", HUB_URL)

PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram")
FORCE_POLLING = os.getenv("FORCE_POLLING", "0") == "1"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "solo_metro.db"))

CAPTCHA_ATTEMPTS = 3
CAPTCHA_LOCK_SECONDS = 60
MAX_WALLETS_FREE = 8
MAX_WALLETS_PREMIUM = 10
FEE_PERCENT = "1%"

CHAINS = {
    "SOL": {
        "name": "Solana",
        "native": "SOL",
        "kind": "sol",
        "emoji": "🟣",
        "autosnipe": True,
        "ape_max": False,
    },
    "BSC": {
        "name": "BNB Smart Chain",
        "native": "BNB",
        "kind": "evm",
        "emoji": "🟡",
        "autosnipe": True,
        "ape_max": True,
    },
    "BASE": {
        "name": "Base",
        "native": "ETH",
        "kind": "evm",
        "emoji": "🔵",
        "autosnipe": True,
        "ape_max": True,
    },
    "ETH": {
        "name": "Ethereum",
        "native": "ETH",
        "kind": "evm",
        "emoji": "♦️",
        "autosnipe": True,
        "ape_max": True,
    },
    "MONAD": {
        "name": "Monad",
        "native": "MON",
        "kind": "evm",
        "emoji": "🟣",
        "autosnipe": False,
        "ape_max": False,
    },
    "SONIC": {
        "name": "Sonic",
        "native": "S",
        "kind": "evm",
        "emoji": "🦔",
        "autosnipe": False,
        "ape_max": False,
    },
    "AVAX": {
        "name": "Avalanche",
        "native": "AVAX",
        "kind": "evm",
        "emoji": "🔺",
        "autosnipe": False,
        "ape_max": True,
    },
    "ARB": {
        "name": "Arbitrum",
        "native": "ETH",
        "kind": "evm",
        "emoji": "🔷",
        "autosnipe": True,
        "ape_max": True,
    },
    "HYPE": {
        "name": "Hyper EVM",
        "native": "HYPE",
        "kind": "evm",
        "emoji": "💚",
        "autosnipe": False,
        "ape_max": False,
    },
    "HOOD": {
        "name": "Robinhood",
        "native": "HOOD",
        "kind": "evm",
        "emoji": "🟢",
        "autosnipe": False,
        "ape_max": False,
    },
    "TRX": {
        "name": "Tron",
        "native": "TRX",
        "kind": "tron",
        "emoji": "🔴",
        "autosnipe": False,
        "ape_max": False,
    },
    "TON": {
        "name": "TON",
        "native": "TON",
        "kind": "ton",
        "emoji": "💎",
        "autosnipe": False,
        "ape_max": False,
    },
}

CHAIN_ORDER = list(CHAINS.keys())

BOT_COMMANDS = [
    ("start", "Open the main menu"),
    ("metro", "Open Solo Metro main menu"),
    ("sniper", "Open the sniper / main menu"),
    ("menu", "Open the main menu"),
    ("home", "Open the main menu"),
    ("bridge", "Bridge tokens via Relay"),
    ("quick", "Quick settings (/quick_ETH for ETH)"),
    ("settings", "Global settings (gas, slippage, Anti-MEV)"),
    ("monitor", "Open the trade monitor"),
    ("summary", "Summary of active trade monitors"),
    ("chains", "Enable or disable chains and set up wallets"),
    ("copytrade", "Copy buys and sells of tracked wallets"),
    ("copy", "Copytrade (alias)"),
    ("signals", "Call channels and auto-buy signals"),
    ("autosnipe", "View active auto-snipes across chains"),
    ("snipe", "Auto snipe (alias)"),
    ("godmode", "God Mode liquidity / method snipes"),
    ("presale", "Pinksale / launchpad presale snipe"),
    ("dca", "DCA buy limits on an interval"),
    ("referral", "Referral stats and options"),
    ("cleartrades", "Clear all tracked tokens"),
    ("orders", "View active limit orders"),
    ("limits", "Limit orders (alias)"),
    ("limit", "Limit orders (alias)"),
    ("pos", "View active positions"),
    ("positions", "Positions (alias)"),
    ("pnl", "P&L / open positions"),
    ("mvp", "View your MVP holdings"),
    ("trending", "Top trending tokens"),
    ("hot", "Trending tokens (alias)"),
    ("pumpfun", "PumpFun cashback claim panel"),
    ("private", "Private bridge via HoudiniSwap"),
    ("relay", "Bridge tokens via Relay"),
    ("debridge", "Bridge tokens via deBridge"),
    ("premium", "Upgrade to Premium"),
    ("subscribe", "Pay subscription / Premium"),
    ("wallets", "View wallets (/wallets_ETH for ETH)"),
    ("wallet", "Wallets (alias)"),
    ("balance", "Native balances on enabled chains"),
    ("bal", "Balances (alias)"),
    ("collect", "Send funds from multiple wallets into one"),
    ("disperse", "Send funds from one wallet to many"),
    ("cashback", "Trading fee rebates"),
    ("claim", "Claim cashback / rewards"),
    ("rewards", "Start and claim rewards"),
    ("campaigns", "Limited-time campaigns"),
    ("competition", "Trading competition"),
    ("import", "Import wallet to another compatible chain"),
    ("export", "Export a wallet private key"),
    ("approve", "Approve the DEX router"),
    ("buysell", "Paste a CA to trade now"),
    ("buy", "Open buy — paste a CA"),
    ("sell", "Open sell — paste a CA"),
    ("trade", "Paste a CA to trade"),
    ("scan", "Scan a CA (tax / honeypot)"),
    ("chart", "Open chart for a pasted CA"),
    ("language", "Switch EN / 中文"),
    ("lang", "Language (alias)"),
    ("cancel", "Cancel the current prompt"),
    ("scraper", "Forward CAs from channels"),
    ("faq", "FAQ and how-to"),
    ("tutorial", "Quick start tutorial"),
    ("docs", "Documentation links"),
    ("ping", "Bot uptime / health"),
    ("status", "Bot status"),
    ("eth", "Ethereum wallets"),
    ("sol", "Solana wallets"),
    ("bsc", "BNB Smart Chain wallets"),
    ("base", "Base wallets"),
    ("arb", "Arbitrum wallets"),
    ("avax", "Avalanche wallets"),
    ("arc", "Bridge assets to USDC on Arc"),
    ("support", "Bot manual and 24/7 live support"),
    ("help", "Summary of useful commands"),
]


def require_token() -> str:
    if not BOT_TOKEN or "your-botfather-token" in BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Create a bot with @BotFather and set the env var."
        )
    return BOT_TOKEN
