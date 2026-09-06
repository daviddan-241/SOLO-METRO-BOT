"""Live token reports: DexScreener, GeckoTerminal, Jupiter, GoPlus. Never raise."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from bot.chainmeta import CHAIN_META, DEX_TO_CHAIN

log = logging.getLogger("solo-metro.market")
DEX = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"
TRENDING = "https://api.dexscreener.com/token-boosts/top/v1"
GECKO = "https://api.geckoterminal.com/api/v2"
JUP_TOKEN = "https://lite-api.jup.ag/tokens/v1/token"

_client: httpx.AsyncClient | None = None

CHAIN_DEX = {code: (meta.get("dex") or "") for code, meta in CHAIN_META.items()}


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=18.0, headers={"User-Agent": "DelugeBot/1.0"})
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
    return f"${n:.8f}".rstrip("0").rstrip(".")


def _fmt_pct(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.2f}%"


def _empty(ca: str, chain: str | None, warning: str = "") -> dict:
    return {
        "ok": True,
        "ca": ca,
        "chain": chain or "ETH",
        "symbol": "TOKEN",
        "name": "Token",
        "price": 0.0,
        "mc": 0.0,
        "liq": 0.0,
        "change": 0.0,
        "dex": "—",
        "pair_url": "",
        "pair_address": "",
        "pairs": [],
        "warning": warning
        or "No live pool yet (dead / pre-launch / very old). You can still trade when liquidity appears.",
        "h1": None,
        "vol": 0,
        "created": None,
        "sources": [],
    }


def pick_pair(pairs: list[dict], prefer_chain: str | None = None) -> Optional[dict]:
    if not pairs:
        return None
    scored = []
    for p in pairs:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        vol = float((p.get("volume") or {}).get("h24") or 0)
        bonus = 1e12 if prefer_chain and DEX_TO_CHAIN.get(p.get("chainId")) == prefer_chain else 0
        scored.append((liq + vol * 0.01 + bonus, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


async def _get_json(url: str, **kwargs) -> Any:
    c = await client()
    try:
        r = await c.get(url, **kwargs)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception as exc:
        log.debug("http %s: %s", url[:80], exc)
        return None


async def fetch_pairs(ca: str) -> list[dict]:
    data = await _get_json(f"{DEX}/latest/dex/tokens/{ca}")
    if isinstance(data, dict):
        return data.get("pairs") or []
    return []


async def search_token(q: str) -> list[dict]:
    data = await _get_json(f"{DEX}/latest/dex/search", params={"q": q})
    if isinstance(data, dict):
        return data.get("pairs") or []
    return []


async def fetch_token_pairs_chain(dex_id: str, ca: str) -> list[dict]:
    if not dex_id:
        return []
    data = await _get_json(f"{DEX}/token-pairs/v1/{dex_id}/{ca}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("pairs") or data.get("data") or []
    return []


def _from_dex_pair(pair: dict, ca: str, prefer_chain: str | None, pairs: list) -> dict:
    chain = DEX_TO_CHAIN.get(pair.get("chainId"), prefer_chain or "ETH")
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    token_addr = base.get("address") or ca
    if ca.lower() == (quote.get("address") or "").lower():
        token_addr = quote.get("address") or ca
        symbol = quote.get("symbol") or "TOKEN"
        name = quote.get("name") or symbol
    else:
        symbol = base.get("symbol") or "TOKEN"
        name = base.get("name") or symbol
    created = pair.get("pairCreatedAt")
    return {
        "ok": True,
        "ca": token_addr,
        "chain": chain,
        "symbol": symbol,
        "name": name,
        "price": float(pair.get("priceUsd") or 0),
        "mc": float(pair.get("marketCap") or pair.get("fdv") or 0),
        "liq": float((pair.get("liquidity") or {}).get("usd") or 0),
        "change": float((pair.get("priceChange") or {}).get("h24") or 0),
        "dex": pair.get("dexId") or "—",
        "pair_url": pair.get("url") or "",
        "pair_address": pair.get("pairAddress") or "",
        "pairs": pairs,
        "warning": "",
        "h1": (pair.get("priceChange") or {}).get("h1"),
        "vol": (pair.get("volume") or {}).get("h24"),
        "created": created,
        "sources": ["DexScreener"],
    }


async def _gecko_search(ca: str, prefer_chain: str | None) -> dict | None:
    data = await _get_json(f"{GECKO}/search/pools", params={"query": ca})
    pools = ((data or {}).get("data") if isinstance(data, dict) else None) or []
    if not pools:
        return None
    best = pools[0]
    attrs = best.get("attributes") or {}
    rel = best.get("relationships") or {}
    net = ((rel.get("network") or {}).get("data") or {}).get("id") or ""
    chain = DEX_TO_CHAIN.get(net, prefer_chain or "ETH")
    name = attrs.get("name") or "Token"
    symbol = (name.split("/")[0] if "/" in name else name)[:16]
    price = 0.0
    try:
        price = float(attrs.get("base_token_price_usd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    liq = 0.0
    try:
        liq = float((attrs.get("reserve_in_usd") or 0))
    except (TypeError, ValueError):
        liq = 0.0
    return {
        "ok": True,
        "ca": ca,
        "chain": chain,
        "symbol": symbol.strip() or "TOKEN",
        "name": name,
        "price": price,
        "mc": 0.0,
        "liq": liq,
        "change": 0.0,
        "dex": attrs.get("dex") or "GeckoTerminal",
        "pair_url": attrs.get("name") and f"https://www.geckoterminal.com/{net}/pools/{best.get('id', '')}" or "",
        "pair_address": attrs.get("address") or "",
        "pairs": [],
        "warning": "",
        "h1": None,
        "vol": attrs.get("volume_usd", {}).get("h24") if isinstance(attrs.get("volume_usd"), dict) else None,
        "created": attrs.get("pool_created_at"),
        "sources": ["GeckoTerminal"],
    }


async def _jupiter_token(ca: str) -> dict | None:
    if ca.startswith("0x"):
        return None
    data = await _get_json(f"{JUP_TOKEN}/{ca}")
    if not isinstance(data, dict) or not (data.get("symbol") or data.get("name")):
        data = await _get_json("https://tokens.jup.ag/token/" + ca)
    if not isinstance(data, dict):
        return None
    symbol = data.get("symbol") or "TOKEN"
    name = data.get("name") or symbol
    if not symbol and not name:
        return None
    return {
        "ok": True,
        "ca": data.get("address") or ca,
        "chain": "SOL",
        "symbol": symbol,
        "name": name,
        "price": 0.0,
        "mc": 0.0,
        "liq": 0.0,
        "change": 0.0,
        "dex": "Jupiter",
        "pair_url": f"https://jup.ag/swap/SOL-{ca}",
        "pair_address": "",
        "pairs": [],
        "warning": "Listed on Jupiter; no DexScreener pool (old / zero-volume still OK).",
        "h1": None,
        "vol": 0,
        "created": None,
        "sources": ["Jupiter"],
    }


async def resolve_token(ca: str, prefer_chain: str | None = None) -> dict:
    ca = (ca or "").strip()
    if not ca:
        return _empty("", prefer_chain, "Paste a token contract address.")
    try:
        return await _resolve_token(ca, prefer_chain)
    except Exception as exc:
        log.warning("resolve_token %s: %s", ca[:18], exc)
        return _empty(ca, prefer_chain, "Lookup timed out — token report still opened so you can buy/sell.")


async def _resolve_token(ca: str, prefer_chain: str | None) -> dict:
    dex_pref = CHAIN_DEX.get(prefer_chain or "", "")
    tasks = [
        fetch_pairs(ca),
        search_token(ca),
        fetch_token_pairs_chain(dex_pref, ca) if dex_pref else fetch_pairs(""),
        _gecko_search(ca, prefer_chain),
        _jupiter_token(ca),
    ]
    if prefer_chain != "SOL" and ca.startswith("0x"):
        extra = []
        for code, dex_id in (("ETH", "ethereum"), ("BSC", "bsc"), ("BASE", "base"), ("ARB", "arbitrum")):
            if dex_id != dex_pref:
                extra.append(fetch_token_pairs_chain(dex_id, ca))
        tasks.extend(extra[:3])
    elif not ca.startswith("0x"):
        tasks.append(fetch_token_pairs_chain("solana", ca))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    pairs: list[dict] = []
    gecko = None
    jup = None
    for item in results:
        if isinstance(item, Exception) or item in (None, True):
            continue
        if isinstance(item, list):
            pairs.extend(item)
        elif isinstance(item, dict) and item.get("sources") == ["GeckoTerminal"]:
            gecko = item
        elif isinstance(item, dict) and item.get("sources") == ["Jupiter"]:
            jup = item

    pair = pick_pair(pairs, prefer_chain)
    if pair:
        info = _from_dex_pair(pair, ca, prefer_chain, pairs)
        if gecko:
            info["sources"] = list(dict.fromkeys(info.get("sources", []) + ["GeckoTerminal"]))
        if jup:
            info["sources"] = list(dict.fromkeys(info.get("sources", []) + ["Jupiter"]))
        if not info.get("price") and gecko and gecko.get("price"):
            info["price"] = gecko["price"]
        return info
    if gecko:
        return gecko
    if jup:
        return jup
    return _empty(
        ca,
        prefer_chain,
        "No DEX pool found (can be 2+ years old, rugged, or never traded). CA is still loaded — you can buy when a pool exists.",
    )


async def goplus(chain: str, ca: str) -> dict:
    meta = CHAIN_META.get(chain) or {}
    cid = meta.get("goplus")
    if not cid:
        return {}
    try:
        data = await _get_json(f"{GOPLUS}/{cid}", params={"contract_addresses": ca.lower()})
        blob = (data or {}).get("result") or {}
        return blob.get(ca.lower()) or {}
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
    mode_l = "🟢 Buy menu" if mode == "buy" else "🔴 Sell menu"
    created = info.get("created")
    age = ""
    if created:
        try:
            import time as _t

            ts = int(created) / (1000 if int(created) > 10_000_000_000 else 1)
            days = max(0, int((_t.time() - ts) / 86400))
            if days >= 365:
                age = f" · {days // 365}y old"
            elif days:
                age = f" · {days}d old"
        except (TypeError, ValueError):
            age = ""
    sources = info.get("sources") or []
    src = f" · {', '.join(sources)}" if sources else ""
    lines = [
        f"📊 <b>${escape(str(info.get('symbol') or 'TOKEN'))}</b> — {escape(str(info.get('name') or ''))}",
        f"{meta.get('emoji', '')} {meta.get('name', chain)} · {escape(str(info.get('dex') or '—'))}{age}{escape(src)}",
        "",
        f"<code>{escape(ca)}</code>",
        "",
        f"💵 Price: <b>{_fmt_usd(info.get('price'))}</b>   📈 24h: <b>{_fmt_pct(info.get('change'))}</b>",
        f"🧢 MC: <b>{_fmt_usd(info.get('mc'))}</b>   💧 Liq: <b>{_fmt_usd(info.get('liq'))}</b>",
        f"📚 24h vol: <b>{_fmt_usd(info.get('vol'))}</b>",
        tax_line,
        "",
        mode_l,
    ]
    if info.get("pair_url") and str(info["pair_url"]).startswith("http"):
        lines.append(f'🔗 <a href="{info["pair_url"]}">Chart</a>')
    if warn:
        lines.append("")
        lines.extend(warn)
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
