"""Deep multi-source token reports. Never raise, never "no info".

Sources (queried in parallel, best-effort):
  1. DexScreener — latest/dex/tokens, search, token-pairs/v1 per chain
     (catches 2yr+ old pools, dead pools, multi-chain same-CA)
  2. GeckoTerminal — search/pools + networks/{net}/tokens/{addr}
  3. Jupiter token list (Solana identity even with zero volume)
  4. RugCheck report (Solana identity + risks, works for ancient mints)
  5. CoinGecko contract endpoint (EVM identity + market data fallback)
  6. Birdeye overview (optional, if BIRDEYE_API_KEY set)
  7. GoPlus token_security (tax / honeypot / holders / verified)
  8. On-chain RPC fallback — EVM symbol/name/decimals/supply via eth_call,
     Solana mint decimals/supply via getAccountInfo. This is what guarantees
     a 2-year-old dead token with zero buys still resolves to real info.
Results are cached in SQLite (live 45s, dead 6h) so repeats open instantly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from bot.chainmeta import CHAIN_META, DEX_TO_CHAIN

log = logging.getLogger("solo-metro.market")
DEX = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"
TRENDING = "https://api.dexscreener.com/token-boosts/top/v1"
GECKO = "https://api.geckoterminal.com/api/v2"
JUP_TOKEN = "https://lite-api.jup.ag/tokens/v1/token"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
COINGECKO = "https://api.coingecko.com/api/v3"

_client: httpx.AsyncClient | None = None

CHAIN_DEX = {code: (meta.get("dex") or "") for code, meta in CHAIN_META.items()}

# GeckoTerminal network slugs per our chain code
GECKO_NET = {
    "SOL": "solana",
    "ETH": "eth",
    "BSC": "bsc",
    "BASE": "base",
    "ARB": "arbitrum",
    "AVAX": "avax",
    "SONIC": "sonic",
}
# CoinGecko asset_platform_id per our chain code
CG_PLATFORM = {
    "ETH": "ethereum",
    "BSC": "binance-smart-chain",
    "BASE": "base",
    "ARB": "arbitrum-one",
    "AVAX": "avalanche",
    "SONIC": "sonic",
}
DEX_IDS_ALL = ["solana", "ethereum", "bsc", "base", "arbitrum", "avalanche", "sonic"]


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(18.0, connect=6.0),
            headers={"User-Agent": "DelugeBot/1.0", "Accept": "application/json"},
        )
    return _client


def _fmt_usd(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.2f}K"
    if n >= 1:
        return f"${n:.4f}"
    if n >= 0.0001:
        return f"${n:.8f}".rstrip("0").rstrip(".")
    # tiny prices (memecoins): significant digits, e.g. $0.0₆1234
    from decimal import Decimal
    d = Decimal(str(n)).normalize()
    s = format(d, "f")
    if "e" in s.lower() or "E" in s:
        return f"${n:.4g}"
    # compress zeros: 0.00000012 -> 0.0₆12
    try:
        frac = s.split(".")[1]
        zeros = len(frac) - len(frac.lstrip("0"))
        sig = frac.lstrip("0")[:4]
        if zeros >= 4:
            subs = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
            return f"$0.0{str(zeros).translate(subs)}{sig}"
    except Exception:
        pass
    return f"${n:.10g}"


def _fmt_pct(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.2f}%"


def _fmt_big(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.2f}K"
    return f"{n:,.0f}" if n >= 100 else f"{n:.4g}"


def _short(ca: str) -> str:
    ca = (ca or "").strip()
    if len(ca) > 12:
        return ca[:6] + "…" + ca[-4:]
    return ca or "TOKEN"


def _empty(ca: str, chain: str | None, warning: str = "") -> dict:
    return {
        "ok": True,
        "ca": ca,
        "chain": chain or "ETH",
        "symbol": _short(ca),
        "name": "Token " + _short(ca),
        "price": 0.0,
        "mc": 0.0,
        "liq": 0.0,
        "change": 0.0,
        "dex": "—",
        "pair_url": "",
        "pair_address": "",
        "pairs": [],
        "warning": warning
        or "No live pool yet (dead / pre-launch / very old). CA is loaded — you can still trade when liquidity appears.",
        "h1": None,
        "vol": 0,
        "created": None,
        "sources": [],
        "decimals": None,
        "supply": 0,
        "holders": 0,
    }


def pick_pair(pairs: list[dict], prefer_chain: str | None = None, ca: str = "") -> Optional[dict]:
    if not pairs:
        return None
    # de-dupe by chain+pairAddress, keep highest-liq instance
    seen: dict[str, dict] = {}
    for p in pairs:
        k = f"{p.get('chainId')}|{p.get('pairAddress')}"
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if k not in seen or liq > float((seen[k].get("liquidity") or {}).get("usd") or 0):
            seen[k] = p
    want = (ca or "").lower()
    scored = []
    for p in seen.values():
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        vol = float((p.get("volume") or {}).get("h24") or 0)
        txns = p.get("txns") or {}
        buys = sum(float((txns.get(k) or {}).get("buys") or 0) for k in ("h1", "h6", "h24"))
        bonus = 1e12 if prefer_chain and DEX_TO_CHAIN.get(p.get("chainId")) == prefer_chain else 0
        # priceUsd is only valid when our token is the BASE side — strongly
        # prefer base-side pairs so we never show another token's price.
        if want:
            base_a = ((p.get("baseToken") or {}).get("address") or "").lower()
            quote_a = ((p.get("quoteToken") or {}).get("address") or "").lower()
            if base_a == want:
                bonus += 1e15
            elif quote_a == want:
                bonus -= 1e15
        scored.append((liq + vol * 0.01 + buys * 10 + bonus, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


async def _get_json(url: str, tries: int = 2, **kwargs) -> Any:
    c = await client()
    last = None
    for i in range(max(1, tries)):
        try:
            r = await c.get(url, **kwargs)
            if r.status_code == 429:
                await asyncio.sleep(0.6 * (i + 1))
                continue
            if r.status_code >= 400:
                return None
            try:
                return r.json()
            except Exception:
                return None
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.25 * (i + 1))
    if last:
        log.debug("http %s: %s", url[:100], last)
    return None


async def fetch_pairs(ca: str) -> list[dict]:
    if not ca:
        return []
    data = await _get_json(f"{DEX}/latest/dex/tokens/{ca}")
    if isinstance(data, dict):
        out = data.get("pairs") or []
        return out if isinstance(out, list) else []
    return []


async def search_token(q: str) -> list[dict]:
    if not q:
        return []
    data = await _get_json(f"{DEX}/latest/dex/search", params={"q": q})
    if isinstance(data, dict):
        out = data.get("pairs") or []
        return out if isinstance(out, list) else []
    return []


async def fetch_token_pairs_chain(dex_id: str, ca: str) -> list[dict]:
    if not dex_id or not ca:
        return []
    data = await _get_json(f"{DEX}/token-pairs/v1/{dex_id}/{ca}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("pairs", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _from_dex_pair(pair: dict, ca: str, prefer_chain: str | None, pairs: list) -> dict:
    chain = DEX_TO_CHAIN.get(pair.get("chainId"), prefer_chain or "ETH")
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    token_addr = base.get("address") or ca
    quote_side = ca.lower() == (quote.get("address") or "").lower() and ca.lower() != (base.get("address") or "").lower()
    if quote_side:
        token_addr = quote.get("address") or ca
        symbol = quote.get("symbol") or _short(ca)
        name = quote.get("name") or symbol
    else:
        symbol = base.get("symbol") or _short(ca)
        name = base.get("name") or symbol
    created = pair.get("pairCreatedAt")
    txns = pair.get("txns") or {}
    # priceUsd belongs to the BASE token — if ours is quote side, don't lie.
    price = 0.0 if quote_side else float(pair.get("priceUsd") or 0)
    warn = "Price from this pool is for the paired token — enriched price shown when available." if quote_side else ""
    return {
        "ok": True,
        "ca": token_addr,
        "chain": chain,
        "symbol": symbol,
        "name": name,
        "price": price,
        "mc": float(pair.get("marketCap") or pair.get("fdv") or 0),
        "liq": float((pair.get("liquidity") or {}).get("usd") or 0),
        "change": float((pair.get("priceChange") or {}).get("h24") or 0),
        "dex": pair.get("dexId") or "—",
        "pair_url": pair.get("url") or "",
        "pair_address": pair.get("pairAddress") or "",
        "pairs": pairs,
        "warning": warn,
        "h1": (pair.get("priceChange") or {}).get("h1"),
        "vol": (pair.get("volume") or {}).get("h24"),
        "created": created,
        "sources": ["DexScreener"],
        "decimals": None,
        "supply": 0,
        "holders": 0,
        "buys_h24": (txns.get("h24") or {}).get("buys", 0),
        "sells_h24": (txns.get("h24") or {}).get("sells", 0),
    }


async def _gecko_search(ca: str, prefer_chain: str | None) -> dict | None:
    data = await _get_json(f"{GECKO}/search/pools", params={"query": ca})
    pools = ((data or {}).get("data") if isinstance(data, dict) else None) or []
    if not pools:
        return None
    # prefer pools on the requested network
    want = (GECKO_NET.get(prefer_chain or "") or "").lower()
    def _net(p: dict) -> str:
        return str((((p.get("relationships") or {}).get("network") or {}).get("data") or {}).get("id") or "").lower()
    pools_sorted = sorted(pools, key=lambda p: 0 if want and _net(p) == want else 1)
    best = pools_sorted[0]
    attrs = best.get("attributes") or {}
    rel = best.get("relationships") or {}
    net = ((rel.get("network") or {}).get("data") or {}).get("id") or ""
    chain = DEX_TO_CHAIN.get(net, prefer_chain or "ETH")
    name = attrs.get("name") or "Token"
    symbol = (name.split("/")[0] if "/" in name else name)[:16]
    try:
        price = float(attrs.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        liq = float(attrs.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        liq = 0.0
    vol = None
    vu = attrs.get("volume_usd")
    if isinstance(vu, dict):
        vol = vu.get("h24")
    pid = str(best.get("id") or "")
    url = f"https://www.geckoterminal.com/{net}/pools/{pid}" if net and pid else ""
    return {
        "ok": True, "ca": ca, "chain": chain,
        "symbol": (symbol.strip() or _short(ca)), "name": name,
        "price": price, "mc": float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0),
        "liq": liq, "change": 0.0,
        "dex": attrs.get("dex") or "GeckoTerminal",
        "pair_url": url, "pair_address": attrs.get("address") or "",
        "pairs": [], "warning": "", "h1": None, "vol": vol,
        "created": attrs.get("pool_created_at"), "sources": ["GeckoTerminal"],
        "decimals": None, "supply": 0, "holders": 0,
    }


async def _gecko_token(ca: str, prefer_chain: str | None) -> dict | None:
    """Direct token lookup — works even with zero pools."""
    nets = []
    if prefer_chain and GECKO_NET.get(prefer_chain):
        nets.append(GECKO_NET[prefer_chain])
    nets += [n for n in ("solana", "eth", "bsc", "base", "arbitrum", "avax", "sonic") if n not in nets]
    is_evm = ca.startswith("0x")
    for net in nets[:4 if is_evm else 2]:
        if is_evm and net == "solana":
            continue
        if not is_evm and net != "solana":
            continue
        data = await _get_json(f"{GECKO}/networks/{net}/tokens/{ca}", tries=1)
        node = ((data or {}).get("data") if isinstance(data, dict) else None) or {}
        attrs = node.get("attributes") or {}
        if not attrs:
            continue
        symbol = attrs.get("symbol") or _short(ca)
        name = attrs.get("name") or symbol
        try:
            price = float((attrs.get("price_usd") or 0))
        except (TypeError, ValueError):
            price = 0.0
        try:
            mc = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
        except (TypeError, ValueError):
            mc = 0.0
        chain = next((c for c, g in GECKO_NET.items() if g == net), prefer_chain or ("SOL" if not is_evm else "ETH"))
        try:
            _dec = int(attrs.get("decimals") or 0)
        except (TypeError, ValueError):
            _dec = 0
        try:
            _raw_sup = float(attrs.get("total_supply") or 0)
        except (TypeError, ValueError):
            _raw_sup = 0.0
        # Gecko total_supply is in raw units — normalize to whole tokens
        _sup = _raw_sup / (10 ** _dec) if _raw_sup and _dec else (_raw_sup if _raw_sup < 1e15 else 0.0)
        return {
            "ok": True, "ca": attrs.get("address") or ca, "chain": chain,
            "symbol": symbol, "name": name, "price": price, "mc": mc,
            "liq": 0.0, "change": 0.0, "dex": "GeckoTerminal",
            "pair_url": "", "pair_address": "", "pairs": [],
            "warning": "No live DEX pool — identity from GeckoTerminal (old / zero-volume token).",
            "h1": None, "vol": 0, "created": None, "sources": ["GeckoTerminal"],
            "decimals": _dec or None, "supply": _sup, "holders": 0,
        }
    return None


async def _jupiter_token(ca: str) -> dict | None:
    if ca.startswith("0x"):
        return None
    data = await _get_json(f"{JUP_TOKEN}/{ca}")
    if not isinstance(data, dict) or not (data.get("symbol") or data.get("name")):
        data = await _get_json("https://tokens.jup.ag/token/" + ca)
    if not isinstance(data, dict):
        return None
    symbol = data.get("symbol") or ""
    name = data.get("name") or symbol
    if not symbol and not name:
        return None
    return {
        "ok": True, "ca": data.get("address") or ca, "chain": "SOL",
        "symbol": symbol or _short(ca), "name": name or symbol or _short(ca),
        "price": 0.0, "mc": 0.0, "liq": 0.0, "change": 0.0, "dex": "Jupiter",
        "pair_url": f"https://jup.ag/swap/SOL-{ca}", "pair_address": "",
        "pairs": [], "warning": "Listed on Jupiter; no DexScreener pool (old / zero-volume still OK).",
        "h1": None, "vol": 0, "created": None, "sources": ["Jupiter"],
        "decimals": data.get("decimals"), "supply": 0, "holders": 0,
        "logo": data.get("logoURI") or "",
    }


async def _rugcheck_token(ca: str) -> dict | None:
    if ca.startswith("0x"):
        return None
    data = await _get_json(f"{RUGCHECK}/{ca}/report", tries=1)
    if not isinstance(data, dict):
        return None
    tok = data.get("tokenMeta") or {}
    symbol = tok.get("symbol") or ""
    name = tok.get("name") or symbol
    if not symbol and not name and not data.get("mint"):
        return None
    return {
        "ok": True, "ca": ca, "chain": "SOL",
        "symbol": symbol or _short(ca), "name": name or symbol or _short(ca),
        "price": float(data.get("price") or 0), "mc": 0.0,
        "liq": float((data.get("totalLPProviders") or 0)) and 0.0 or 0.0,
        "change": 0.0, "dex": "RugCheck",
        "pair_url": f"https://rugcheck.xyz/tokens/{ca}", "pair_address": "",
        "pairs": [], "warning": "No live DEX pool — identity from RugCheck (old / zero-volume token).",
        "h1": None, "vol": 0, "created": None, "sources": ["RugCheck"],
        "decimals": (tok.get("decimals")), "supply": float(tok.get("totalSupply") or 0), "holders": int(data.get("totalHolders") or 0),
    }


async def _coingecko_token(ca: str, prefer_chain: str | None) -> dict | None:
    if not ca.startswith("0x"):
        return None
    plats: list[str] = []
    if prefer_chain and CG_PLATFORM.get(prefer_chain):
        plats.append(CG_PLATFORM[prefer_chain])
    plats += [p for p in ("ethereum", "binance-smart-chain", "base", "arbitrum-one", "avalanche", "sonic") if p not in plats]
    cg_key = (os.getenv("COINGECKO_API_KEY") or "").strip()
    cg_headers = {"x-cg-demo-api-key": cg_key} if cg_key else None
    for plat in plats[:3]:
        data = await _get_json(f"{COINGECKO}/coins/{plat}/contract/{ca.lower()}", tries=1,
                               params={"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false"},
                               headers=cg_headers or {})
        if not isinstance(data, dict) or data.get("error"):
            continue
        md = data.get("market_data") or {}
        try:
            price = float((md.get("current_price") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            mc = float((md.get("market_cap") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            mc = 0.0
        try:
            vol = float((md.get("total_volume") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        try:
            chg = float(md.get("price_change_percentage_24h") or 0)
        except (TypeError, ValueError):
            chg = 0.0
        chain = next((c for c, p in CG_PLATFORM.items() if p == plat), prefer_chain or "ETH")
        det = data.get("detail_platforms") or {}
        dec = ((det.get(plat) or {}).get("decimal_place"))
        return {
            "ok": True, "ca": ca, "chain": chain,
            "symbol": str(data.get("symbol") or _short(ca)).upper(),
            "name": data.get("name") or _short(ca),
            "price": price, "mc": mc, "liq": 0.0, "change": chg,
            "dex": "CoinGecko", "pair_url": data.get("links", {}).get("homepage", [""])[0] if isinstance(data.get("links"), dict) else "",
            "pair_address": "", "pairs": [],
            "warning": "No live DEX pool — data from CoinGecko (old / zero-volume token).",
            "h1": None, "vol": vol, "created": None, "sources": ["CoinGecko"],
            "decimals": dec, "supply": float(md.get("total_supply") or 0), "holders": 0,
        }
    return None


async def _birdeye_token(ca: str, prefer_chain: str | None) -> dict | None:
    key = (os.getenv("BIRDEYE_API_KEY") or "").strip()
    if not key:
        return None
    cmap = {"SOL": "solana", "ETH": "ethereum", "BSC": "bsc", "BASE": "base", "ARB": "arbitrum", "AVAX": "avalanche"}
    chain = cmap.get(prefer_chain or ("SOL" if not ca.startswith("0x") else "ETH"), "solana")
    data = await _get_json("https://public-api.birdeye.so/defi/token_overview",
                           params={"address": ca, "chain": chain},
                           headers={"X-API-KEY": key, "x-chain": chain}, tries=1)
    d = ((data or {}).get("data")) if isinstance(data, dict) else None
    if not isinstance(d, dict) or not (d.get("symbol") or d.get("name")):
        return None
    return {
        "ok": True, "ca": ca, "chain": prefer_chain or ("SOL" if not ca.startswith("0x") else "ETH"),
        "symbol": d.get("symbol") or _short(ca), "name": d.get("name") or _short(ca),
        "price": float(d.get("price") or 0), "mc": float(d.get("mc") or d.get("fdv") or 0),
        "liq": float(d.get("liquidity") or 0), "change": float(d.get("priceChange24hPercent") or 0),
        "dex": "Birdeye", "pair_url": "", "pair_address": "", "pairs": [],
        "warning": "", "h1": None, "vol": float(d.get("v24hUSD") or 0),
        "created": None, "sources": ["Birdeye"],
        "decimals": d.get("decimals"), "supply": float(d.get("supply") or 0), "holders": int(d.get("holder") or 0),
    }


def _onchain_evm_sync(chain: str, ca: str) -> dict | None:
    """eth_call symbol/name/decimals/totalSupply — works for dead tokens."""
    try:
        from web3 import Web3
        from bot.chainmeta import rpcs
    except Exception:
        return None
    urls = rpcs(chain)
    if not urls:
        return None
    ABI = [
        {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    ]
    ABI_B32 = [
        {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
    ]

    def _b32(v: Any) -> str:
        try:
            if isinstance(v, (bytes, bytearray)):
                return bytes(v).rstrip(b"\x00").decode("utf-8", "ignore").strip()
            return str(v or "").strip()
        except Exception:
            return ""
    last = None
    for url in urls[:3]:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
            if not w3.is_connected():
                continue
            c = w3.eth.contract(address=Web3.to_checksum_address(ca.lower()), abi=ABI)
            try:
                code = w3.eth.get_code(Web3.to_checksum_address(ca.lower()))
                if not code or code == b"":
                    return None
            except Exception:
                pass
            try:
                sym = c.functions.symbol().call()
            except Exception:
                sym = ""
            try:
                name = c.functions.name().call()
            except Exception:
                name = ""
            if not sym or not name:
                # 2016-2018 era tokens (MKR et al.) return bytes32
                try:
                    cb = w3.eth.contract(address=Web3.to_checksum_address(ca.lower()), abi=ABI_B32)
                    if not sym:
                        try:
                            sym = _b32(cb.functions.symbol().call())
                        except Exception:
                            pass
                    if not name:
                        try:
                            name = _b32(cb.functions.name().call())
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                dec = int(c.functions.decimals().call())
            except Exception:
                dec = 18
            try:
                raw = int(c.functions.totalSupply().call())
                supply = raw / (10 ** dec) if dec is not None else 0
            except Exception:
                supply = 0
            if not sym and not name:
                continue
            return {"symbol": sym or _short(ca), "name": name or sym or _short(ca),
                    "decimals": dec, "supply": supply}
        except Exception as exc:
            last = exc
            continue
    if last:
        log.debug("onchain evm %s:%s %s", chain, ca[:10], last)
    return None


async def _onchain_evm(ca: str, prefer_chain: str | None) -> dict | None:
    if not ca.startswith("0x") or len(ca) != 42:
        return None
    order = []
    if prefer_chain:
        order.append(prefer_chain)
    order += [c for c in ("ETH", "BSC", "BASE", "ARB", "AVAX", "SONIC") if c not in order]
    for chain in order[:4]:
        try:
            meta = CHAIN_META.get(chain) or {}
            if meta.get("chain_id", 0) == 0 and chain not in ("ETH", "BSC", "BASE", "ARB", "AVAX"):
                pass
            res = await asyncio.to_thread(_onchain_evm_sync, chain, ca)
        except Exception:
            res = None
        if res:
            return {
                "ok": True, "ca": ca, "chain": chain,
                "symbol": res["symbol"], "name": res["name"],
                "price": 0.0, "mc": 0.0, "liq": 0.0, "change": 0.0,
                "dex": "On-chain", "pair_url": "", "pair_address": "",
                "pairs": [], "warning": "No DEX pool anywhere — identity read directly from the contract (old / dead / pre-launch token).",
                "h1": None, "vol": 0, "created": None, "sources": ["On-chain"],
                "decimals": res.get("decimals"), "supply": res.get("supply") or 0, "holders": 0,
            }
    return None


async def _onchain_sol(ca: str) -> dict | None:
    if ca.startswith("0x"):
        return None
    try:
        from bot.engine import sol_rpc
    except Exception:
        return None
    try:
        res = await sol_rpc("getAccountInfo", [ca, {"encoding": "jsonParsed"}])
    except Exception:
        return None
    val = (res or {}).get("value") if isinstance(res, dict) else None
    data = (val or {}).get("data") if isinstance(val, dict) else None
    parsed = (data or {}).get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict) or parsed.get("type") != "mint":
        return None
    try:
        dec = int(info.get("decimals") or 0)
    except (TypeError, ValueError):
        dec = 0
    try:
        supply = float(info.get("supply") or 0) / (10 ** dec) if dec else 0
    except (TypeError, ValueError):
        supply = 0
    symbol, name = None, None
    try:
        symbol, name = await _metaplex_identity(ca)
    except Exception:
        pass
    if not symbol:
        symbol = _short(ca)
    if not name:
        name = f"Token {symbol}"
    return {
        "ok": True, "ca": ca, "chain": "SOL",
        "symbol": symbol, "name": name,
        "price": 0.0, "mc": 0.0, "liq": 0.0, "change": 0.0,
        "dex": "On-chain", "pair_url": f"https://solscan.io/token/{ca}",
        "pair_address": "", "pairs": [],
        "warning": "Mint exists on-chain but has no indexed pool/metadata (very old / dead token).",
        "h1": None, "vol": 0, "created": None, "sources": ["On-chain"],
        "decimals": dec, "supply": supply, "holders": 0,
    }


METAPLEX_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


async def _metaplex_identity(ca: str) -> tuple[str | None, str | None]:
    """Read the REAL name/symbol from the Metaplex metadata account via RPC —
    works for ANY mint that ever existed."""
    import base64 as _b64

    from solders.pubkey import Pubkey

    from bot.engine import sol_rpc

    mint = Pubkey.from_string(ca)
    meta_pda, _ = Pubkey.find_program_address(
        [b"metadata", bytes(Pubkey.from_string(METAPLEX_PROGRAM)), bytes(mint)],
        Pubkey.from_string(METAPLEX_PROGRAM),
    )
    res = await sol_rpc("getAccountInfo", [str(meta_pda), {"encoding": "base64"}])
    val = (res or {}).get("value") if isinstance(res, dict) else None
    data = (val or {}).get("data") if isinstance(val, dict) else None
    b64 = (data or [])[0] if isinstance(data, list) and data else None
    if not b64:
        return None, None
    raw = _b64.b64decode(b64)
    pos = 65

    def _read_str(p):
        if p + 4 > len(raw):
            return "", p
        n = int.from_bytes(raw[p:p + 4], "little")
        p += 4
        s = raw[p:p + n].decode("utf-8", "ignore").rstrip("\x00").strip()
        return s, p + n

    name, pos = _read_str(pos)
    symbol, pos = _read_str(pos)
    return (symbol or None), (name or None)


def _merge_identity(base: dict, extra: dict | None) -> dict:
    if not extra:
        return base
    if (not base.get("price")) and extra.get("price"):
        base["price"] = extra["price"]
    if (not base.get("mc")) and extra.get("mc"):
        base["mc"] = extra["mc"]
    if (not base.get("liq")) and extra.get("liq"):
        base["liq"] = extra["liq"]
    if base.get("symbol") in (None, "", "TOKEN") or base.get("symbol", "").endswith("…"):
        if extra.get("symbol"):
            base["symbol"] = extra["symbol"]
    if (not base.get("name")) or base.get("name", "").startswith("Token "):
        if extra.get("name"):
            base["name"] = extra["name"]
    if base.get("decimals") in (None, "") and extra.get("decimals") not in (None, ""):
        base["decimals"] = extra.get("decimals")
    if not base.get("supply") and extra.get("supply"):
        base["supply"] = extra.get("supply")
    if not base.get("holders") and extra.get("holders"):
        base["holders"] = extra.get("holders")
    if not base.get("pair_url") and extra.get("pair_url"):
        base["pair_url"] = extra.get("pair_url")
    for s in extra.get("sources") or []:
        if s not in (base.get("sources") or []):
            base.setdefault("sources", []).append(s)
    return base


async def resolve_token(ca: str, prefer_chain: str | None = None) -> dict:
    ca = (ca or "").strip()
    if not ca:
        return _empty("", prefer_chain, "Paste a token contract address.")
    # fast path: fresh cache
    try:
        from bot import db as _db
        hit = _db.token_cache_get(ca)
        if hit and hit.get("ok"):
            if prefer_chain and hit.get("chain") != prefer_chain and (hit.get("price") or hit.get("liq")):
                pass  # live data exists but for another chain — re-resolve
            else:
                hit["ca"] = hit.get("ca") or ca
                return hit
    except Exception:
        pass
    try:
        info = await asyncio.wait_for(_resolve_token(ca, prefer_chain), timeout=40)
    except asyncio.TimeoutError:
        log.warning("resolve_token timeout %s", ca[:18])
        info = _empty(ca, prefer_chain, "Lookup timed out — token report still opened so you can buy/sell.")
    except Exception as exc:
        log.warning("resolve_token %s: %s", ca[:18], exc)
        info = _empty(ca, prefer_chain, "Lookup timed out — token report still opened so you can buy/sell.")
    # stale cache as ultimate fallback identity
    if (info.get("symbol") or "").endswith("…") or info.get("symbol") in ("TOKEN", None):
        try:
            from bot import db as _db2
            stale = _db2.token_cache_get(ca)
            if stale and stale.get("symbol"):
                info = _merge_identity(info, stale)
        except Exception:
            pass
    try:
        from bot import db as _db3
        _db3.token_cache_put(ca, info.get("chain") or prefer_chain or "ETH", info)
    except Exception:
        pass
    return info


async def _resolve_token(ca: str, prefer_chain: str | None) -> dict:
    is_evm = ca.startswith("0x")
    dex_pref = CHAIN_DEX.get(prefer_chain or "", "")

    tasks: list = [fetch_pairs(ca), search_token(ca)]
    # per-chain token-pairs sweep (multi-chain same-CA + ancient pools)
    dex_ids = ([dex_pref] if dex_pref else []) + [d for d in DEX_IDS_ALL if d != dex_pref]
    if is_evm:
        dex_ids = [d for d in dex_ids if d != "solana"] or dex_ids
    else:
        dex_ids = ["solana"]
    for did in dex_ids[:4]:
        tasks.append(fetch_token_pairs_chain(did, ca))
    tasks.append(_gecko_search(ca, prefer_chain))
    tasks.append(_gecko_token(ca, prefer_chain))
    if not is_evm:
        tasks.append(_jupiter_token(ca))
        tasks.append(_rugcheck_token(ca))
        tasks.append(_onchain_sol(ca))
    else:
        tasks.append(_coingecko_token(ca, prefer_chain))
    tasks.append(_birdeye_token(ca, prefer_chain))
    # on-chain EVM last (slower) — still in parallel
    if is_evm:
        tasks.append(_onchain_evm(ca, prefer_chain))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    pairs: list[dict] = []
    enrichers: list[dict] = []
    for item in results:
        if isinstance(item, Exception) or item is None:
            continue
        if isinstance(item, list):
            pairs.extend(item)
        elif isinstance(item, dict) and item.get("ok"):
            enrichers.append(item)

    pair = pick_pair(pairs, prefer_chain, ca)
    if pair:
        info = _from_dex_pair(pair, ca, prefer_chain, pairs)
        for e in enrichers:
            info = _merge_identity(info, e)
        if not info.get("warning") and (info.get("buys_h24") == 0 and float(info.get("vol") or 0) == 0):
            info["warning"] = "No buys in 24h (old / inactive pool) — quotes may be stale, slippage may be high."
        return info

    # no DEX pair anywhere — return best identity (never empty)
    if enrichers:
        # prefer one with symbol + (price or supply or holders)
        def _score(e: dict) -> tuple:
            sym = 0 if (e.get("symbol") and not str(e.get("symbol")).endswith("…")) else 1
            has_data = 0 if (e.get("price") or e.get("supply") or e.get("holders")) else 1
            chain_match = 0 if (prefer_chain and e.get("chain") == prefer_chain) else 1
            src_rank = {"Birdeye": 0, "CoinGecko": 1, "GeckoTerminal": 2, "Jupiter": 3, "RugCheck": 4, "On-chain": 5}.get((e.get("sources") or [""])[0], 9)
            return (sym, has_data, chain_match, src_rank)
        enrichers.sort(key=_score)
        info = dict(enrichers[0])
        for e in enrichers[1:]:
            info = _merge_identity(info, e)
        info["pairs"] = []
        if prefer_chain and not info.get("price") and not info.get("liq"):
            # keep detected chain if enricher found one, else honor preference
            if info.get("sources") == ["On-chain"] and info.get("chain") != prefer_chain:
                pass
        return info

    return _empty(
        ca, prefer_chain,
        "No DEX pool found (can be 2+ years old, rugged, or never traded). CA is still loaded — you can buy when a pool exists.",
    )


async def goplus(chain: str, ca: str) -> dict:
    meta = CHAIN_META.get(chain) or {}
    cid = meta.get("goplus")
    if not cid:
        # Solana: RugCheck already covers; still try GoPlus solana id 101? (unsupported) -> skip
        return {}
    try:
        data = await _get_json(f"{GOPLUS}/{cid}", params={"contract_addresses": ca.lower()})
        blob = (data or {}).get("result") or {}
        return blob.get(ca.lower()) or blob.get(ca) or {}
    except Exception as exc:
        log.debug("goplus: %s", exc)
        return {}


def format_report(info: dict, tax: dict | None, mode: str, extra: str = "") -> str:
    from html import escape

    from bot.config import CHAINS

    chain = info.get("chain") or "ETH"
    meta = CHAINS.get(chain, {})
    ca = info.get("ca") or ""
    warn = []
    if info.get("warning"):
        warn.append(str(info["warning"]))
    tax_line = "🧾 Tax: —"
    holders = info.get("holders") or 0
    if tax:
        honeypot = str(tax.get("is_honeypot") or "0")
        if honeypot == "1":
            warn.append("⚠️ GoPlus flagged this as a possible honeypot.")
        buy_t = tax.get("buy_tax")
        sell_t = tax.get("sell_tax")
        if buy_t not in (None, "") or sell_t not in (None, ""):
            tax_line = f"🧾 Tax: Buy {buy_t or '—'}% / Sell {sell_t or '—'}%"
        if str(tax.get("is_blacklisted") or "0") == "1":
            warn.append("⚠️ Blacklist function detected.")
        if str(tax.get("is_open_source") or "1") == "0":
            warn.append("⚠️ Contract is not verified/open source.")
        try:
            if int(float(tax.get("holder_count") or 0)) > 0:
                holders = int(float(tax.get("holder_count")))
        except (TypeError, ValueError):
            pass
        try:
            lp = float(tax.get("lp_holder_count") or 0)
            if lp == 0 and float(info.get("liq") or 0) > 0:
                warn.append("⚠️ No LP holders detected — liquidity may be unlocked/burned.")
        except (TypeError, ValueError):
            pass
    mode_l = "🟢 Buy menu" if mode == "buy" else "🔴 Sell menu"
    created = info.get("created")
    age = ""
    if created:
        try:
            import time as _t
            from datetime import datetime

            if isinstance(created, str) and ("-" in created or "T" in created):
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                days = max(0, int((_t.time() - dt.timestamp()) / 86400))
            else:
                ts = int(created) / (1000 if int(created) > 10_000_000_000 else 1)
                days = max(0, int((_t.time() - ts) / 86400))
            if days >= 365:
                age = f" · {days // 365}y {days % 365 // 30}mo old" if days % 365 >= 30 else f" · {days // 365}y old"
            elif days >= 30:
                age = f" · {days // 30}mo old"
            elif days:
                age = f" · {days}d old"
            else:
                age = " · launched <24h"
        except (TypeError, ValueError):
            age = ""
    sources = info.get("sources") or []
    src = f" · {', '.join(sources)}" if sources else ""
    n_pairs = len(info.get("pairs") or [])
    pairs_s = f" · {n_pairs} pools" if n_pairs > 1 else ""
    lines = [
        f"📊 <b>${escape(str(info.get('symbol') or 'TOKEN'))}</b> — {escape(str(info.get('name') or ''))}",
        f"{meta.get('emoji', '')} {meta.get('name', chain)} · {escape(str(info.get('dex') or '—'))}{age}{pairs_s}{escape(src)}",
        "",
        f"<code>{escape(ca)}</code>",
        "",
        f"💵 Price: <b>{_fmt_usd(info.get('price'))}</b>   📈 24h: <b>{_fmt_pct(info.get('change'))}</b>",
        f"🧢 MC: <b>{_fmt_usd(info.get('mc'))}</b>   💧 Liq: <b>{_fmt_usd(info.get('liq'))}</b>",
        f"📚 24h vol: <b>{_fmt_usd(info.get('vol'))}</b>",
    ]
    sub = []
    if holders:
        sub.append(f"👥 {holders:,}")
    if info.get("supply"):
        sub.append(f"🔢 Supply {_fmt_big(info.get('supply'))}")
    if info.get("buys_h24") is not None and (info.get("buys_h24") or info.get("sells_h24")):
        sub.append(f"🟢{info.get('buys_h24', 0)}/🔴{info.get('sells_h24', 0)} 24h")
    if sub:
        lines.append("  ".join(sub))
    lines.append(tax_line)
    lines.append("")
    lines.append(mode_l)
    if info.get("pair_url") and str(info["pair_url"]).startswith("http"):
        lines.append(f"🔗 <a href=\"{info['pair_url']}\">Chart</a>")
    if warn:
        lines.append("")
        lines.extend(warn[:4])
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


async def trending_text(limit: int = 12) -> str:
    data = await _get_json(TRENDING)
    items = data if isinstance(data, list) else []
    if not items:
        pairs = await search_token("SOL")
        body = ["🔥 <b>Top trending tokens</b>\n"]
        for p in pairs[:limit]:
            base = p.get("baseToken") or {}
            chain = DEX_TO_CHAIN.get(p.get("chainId"), "?")
            body.append(
                f"• {chain} <b>${base.get('symbol')}</b> {_fmt_usd(p.get('priceUsd'))} "
                f"liq {_fmt_usd((p.get('liquidity') or {}).get('usd'))}\n"
                f"<code>{base.get('address')}</code>"
            )
        return "\n".join(body) if len(body) > 1 else "🔥 No trending feed right now."
    lines = ["🔥 <b>Top trending tokens</b>\n"]
    seen = set()
    for it in items:
        tok = (it.get("tokenAddress") or "").strip()
        if not tok or tok in seen:
            continue
        seen.add(tok)
        chain = DEX_TO_CHAIN.get(it.get("chainId"), it.get("chainId") or "?")
        lines.append(f"• {chain} <code>{tok}</code>")
        if len(lines) >= limit + 1:
            break
    return "\n".join(lines) if len(lines) > 1 else "🔥 No trending tokens right now."
