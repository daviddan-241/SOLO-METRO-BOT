import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Deluge").strip() or "Deluge"
BOT_HANDLE = os.getenv("BOT_HANDLE", "@DelugeBot").strip()
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@DelugeSupport").strip()

HUB_URL = os.getenv("HUB_URL", "https://t.me/SoloMetroHub")
UPDATES_URL = os.getenv("UPDATES_URL", "https://t.me/SoloMetroUpdates")
TWITTER_URL = os.getenv("TWITTER_URL", "https://x.com/SoloMetroBot")
DOCS_URL = os.getenv("DOCS_URL", "https://docs.solometro.bot")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/SoloMetroSupport")
TOS_URL = os.getenv("TOS_URL", "https://docs.solometro.bot/tos")
MORE_LINKS_URL = os.getenv("MORE_LINKS_URL", HUB_URL)

# Paid call channel (the product people subscribe for)
CALL_CHANNEL = (os.getenv("CALL_CHANNEL") or "").strip().lstrip("@")
CALL_CHANNEL_URL = (os.getenv("CALL_CHANNEL_URL") or "").strip()
if not CALL_CHANNEL_URL and CALL_CHANNEL:
    CALL_CHANNEL_URL = f"https://t.me/{CALL_CHANNEL}"

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

# Visible Telegram menu — Maestro order and wording. Extra aliases still work when typed.
BOT_COMMANDS = [
    ("start", "Open the main menu"),
    ("bridge", "Bridge tokens via Relay"),
    ("quick", "Quick settings for all chains (/quick_ETH for ETH)"),
    ("monitor", "Open the trade monitor"),
    ("summary", "Summary of active trade monitors"),
    ("chains", "Enable or disable chains and set up wallets"),
    ("autosnipe", "View active auto-snipes across chains"),
    ("referral", "Referral stats and options"),
    ("cleartrades", "Clear all tracked tokens"),
    ("orders", "View active limit orders"),
    ("pos", "View active positions"),
    ("mvp", "View your MVP holdings"),
    ("trending", "Top trending tokens (Premium only)"),
    ("pumpfun", "PumpFun cashback claim panel"),
    ("private", "Private bridge via HoudiniSwap"),
    ("relay", "Bridge tokens via Relay"),
    ("debridge", "Bridge tokens via deBridge"),
    ("premium", "Upgrade to Premium"),
    ("wallets", "View wallets for all chains (/wallets_ETH for ETH)"),
    ("collect", "Send funds from multiple wallets into one"),
    ("disperse", "Send funds from one wallet to many"),
    ("cashback", "Trading fee rebates (old menu: /rewards)"),
    ("rewards", "Start and claim rewards"),
    ("import", "Import wallet to another compatible chain"),
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
