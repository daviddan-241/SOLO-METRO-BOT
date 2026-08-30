"""Live token reports via DexScreener + GoPlus."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from bot.chainmeta import DEX_TO_CHAIN, CHAIN_META

log = logging.getLogger("solo-metro.market")
DEX = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"
TRENDING = "https://api.dexscreener.com/token-boosts/top/v1"

_client: httpx.AsyncClient | None = None


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "SoloMetroBot/1.0"})
    return _client


def _fmt_usd(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
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


def pick_pair(pairs: list[dict], prefer_chain: str | None = None) -> Optional[dict]:
    if not pairs:
        return None
    scored = []
    for p in pairs:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        bonus = 1e12 if prefer_chain and DEX_TO_CHAIN.get(p.get("chainId")) == prefer_chain else 0
        scored.append((liq + bonus, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


async def fetch_pairs(ca: str) -> list[dict]:
    c = await client()
    url = f"{DEX}/latest/dex/tokens/{ca}"
    try:
        r = await c.get(url)
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception as exc:
        log.warning("dexscreener %s: %s", ca[:12], exc)
        return []


async def resolve_token(ca: str, prefer_chain: str | None = None) -> dict:
    pairs = await fetch_pairs(ca)
    pair = pick_pair(pairs, prefer_chain)
    if not pair:
        search = await search_token(ca)
        pair = pick_pair(search, prefer_chain)
    if not pair:
        return {
            "ok": False,
            "ca": ca,
            "chain": prefer_chain or "ETH",
            "symbol": "UNKNOWN",
            "name": "Unknown token",
            "price": 0.0,
            "mc": 0.0,
            "liq": 0.0,
            "change": 0.0,
            "dex": "—",
            "pair_url": "",
            "pairs": [],
            "warning": "No DEX pair found yet (may be pre-launch).",
        }
    chain = DEX_TO_CHAIN.get(pair.get("chainId"), prefer_chain or "ETH")
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    token_addr = base.get("address") or ca
    if ca.lower() == (quote.get("address") or "").lower():
        token_addr = quote.get("address")
        symbol = quote.get("symbol") or "?"
        name = quote.get("name") or symbol
    else:
        symbol = base.get("symbol") or "?"
        name = base.get("name") or symbol
    price = float(pair.get("priceUsd") or 0)
    mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    ch = (pair.get("priceChange") or {}).get("h24")
    return {
        "ok": True,
        "ca": token_addr,
        "chain": chain,
        "symbol": symbol,
        "name": name,
        "price": price,
        "mc": mc,
        "liq": liq,
        "change": float(ch or 0),
        "dex": pair.get("dexId") or "—",
        "pair_url": pair.get("url") or "",
        "pair_address": pair.get("pairAddress") or "",
        "pairs": pairs,
        "warning": "",
        "h1": (pair.get("priceChange") or {}).get("h1"),
        "vol": (pair.get("volume") or {}).get("h24"),
    }


async def search_token(q: str) -> list[dict]:
    c = await client()
    try:
        r = await c.get(f"{DEX}/latest/dex/search", params={"q": q})
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception:
        return []


async def goplus(chain: str, ca: str) -> dict:
    meta = CHAIN_META.get(chain) or {}
    cid = meta.get("goplus")
    if not cid:
        return {}
    c = await client()
    try:
        r = await c.get(f"{GOPLUS}/{cid}", params={"contract_addresses": ca.lower()})
        r.raise_for_status()
        data = r.json().get("result") or {}
        return data.get(ca.lower()) or {}
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
        warn.append(info["warning"])
    if tax:
        honeypot = str(tax.get("is_honeypot") or "0")
        if honeypot == "1":
            warn.append("⚠️ GoPlus flagged this as a possible honeypot.")
        buy_t = tax.get("buy_tax")
        sell_t = tax.get("sell_tax")
        if buy_t not in (None, "") or sell_t not in (None, ""):
            tax_line = f"🧾 Tax: Buy {buy_t or '—'}% / Sell {sell_t or '—'}%"
        else:
            tax_line = "🧾 Tax: —"
        if str(tax.get("is_blacklisted") or "0") == "1":
            warn.append("⚠️ Blacklist function detected.")
        if str(tax.get("is_open_source") or "1") == "0":
            warn.append("⚠️ Contract is not verified/open source.")
    else:
        tax_line = "🧾 Tax: —"
    mode_l = "🟢 Buy menu" if mode == "buy" else "🔴 Sell menu"
    lines = [
        f"📊 <b>${escape(str(info.get('symbol') or '?'))}</b> — {escape(str(info.get('name') or ''))}",
        f"{meta.get('emoji', '')} {meta.get('name', chain)} · {escape(str(info.get('dex') or '—'))}",
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
    if info.get("pair_url"):
        lines.append(f"🔗 <a href=\"{info['pair_url']}\">Chart</a>")
    if warn:
        lines.append("")
        lines.extend(warn)
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


async def trending_text(limit: int = 12) -> str:
    c = await client()
    try:
        r = await c.get(TRENDING)
        r.raise_for_status()
        items = r.json() if isinstance(r.json(), list) else []
    except Exception:
        # fallback: search hot names
        pairs = await search_token("SOL")
        items = []
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
        desc = it.get("description") or it.get("url") or ""
        lines.append(f"• {chain} <code>{tok}</code>")
        if len(lines) >= limit + 1:
            break
        _ = desc
    return "\n".join(lines) if len(lines) > 1 else "🔥 No trending tokens right now."
