"""Background: limit orders, autosnipe, copytrade, trade-monitor refresh."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from bot import db
from bot.chainmeta import CHAIN_META, SOL_NATIVE
from bot.config import CHAINS
from bot.engine import execute_buy, execute_sell, settings_of, sol_rpc, connect_w3
from bot.market import resolve_token

log = logging.getLogger("solo-metro.worker")
_copy_seen: dict[int, str] = {}
_snipe_lock: set[int] = set()


async def notify(bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log.debug("notify %s: %s", user_id, exc)


async def tick_orders(bot) -> None:
    with db.connect() as con:
        rows = con.execute("SELECT * FROM orders WHERE status='active'").fetchall()
    for r in rows:
        o = dict(r)
        try:
            ttype = (o.get("trigger_type") or "price").lower()
            if ttype == "dca":
                try:
                    interval_min = float(str(o["trigger_value"]).split("|")[0])
                except Exception:
                    interval_min = 60.0
                last = float(o.get("last_fire") or 0)
                if last and (time.time() - last) < interval_min * 60:
                    continue
                amt = Decimal(str(o["amount"] or "0") or "0")
                if amt <= 0:
                    continue
                res = await execute_buy(o["user_id"], o["chain"], o["token"], amt, multi=True)
                db.update_order(o["id"], last_fire=time.time())
                await notify(
                    bot,
                    o["user_id"],
                    f"📅 <b>DCA buy</b> {o['chain']} every {interval_min:g}m\n<code>{o['token']}</code>\n"
                    + "\n".join(res),
                )
                continue
            info = await resolve_token(o["token"], o["chain"])
            price = Decimal(str(info.get("price") or 0))
            if price <= 0:
                continue
            trigger = Decimal(str(o["trigger_value"]).replace("x", "").replace("%", "").strip())
            side = o["side"]
            hit = False
            if side == "buy" and price <= trigger:
                hit = True
            elif side == "sell":
                # trigger_value can be usd price or 2x multiplier stored as price
                if "x" in str(o["trigger_value"]).lower():
                    # skip multiplier without basis; treat as usd if numeric else skip
                    hit = False
                elif price >= trigger:
                    hit = True
            if not hit:
                continue
            amt = Decimal(str(o["amount"]).replace("%", "") or "0")
            if side == "buy":
                res = await execute_buy(o["user_id"], o["chain"], o["token"], amt, multi=True)
            else:
                pct = float(str(o["amount"]).replace("%", "") or 100)
                if "%" in str(o["amount"]):
                    res = await execute_sell(o["user_id"], o["chain"], o["token"], None, pct, multi=True)
                else:
                    res = await execute_sell(o["user_id"], o["chain"], o["token"], amt, None, multi=True)
            db.update_order_status(o["id"], "filled")
            await notify(
                bot,
                o["user_id"],
                f"🕓 <b>Limit {side} filled</b> {o['chain']}\n<code>{o['token']}</code>\n" + "\n".join(res),
            )
        except Exception as exc:
            log.warning("order %s: %s", o.get("id"), exc)


async def tick_snipes(bot) -> None:
    items = []
    with db.connect() as con:
        rows = con.execute("SELECT * FROM autosnipe WHERE enabled=1").fetchall()
        items = [dict(r) for r in rows]
    for it in items:
        if it["id"] in _snipe_lock:
            continue
        try:
            info = await resolve_token(it["token"], it["chain"])
            if not info.get("ok") or float(info.get("liq") or 0) <= 0:
                continue
            _snipe_lock.add(it["id"])
            amt = Decimal(str(it["amount"] or "0.05"))
            res = await execute_buy(it["user_id"], it["chain"], it["token"], amt, multi=True)
            db.update_snipe(it["id"], enabled=0)
            await notify(
                bot,
                it["user_id"],
                f"🎯 <b>Auto-snipe fired</b> {it['chain']} ${info.get('symbol')}\n<code>{it['token']}</code>\n"
                + "\n".join(res),
            )
        except Exception as exc:
            log.warning("snipe %s: %s", it.get("id"), exc)
            _snipe_lock.discard(it["id"])


async def _copy_evm(it: dict, bot) -> None:
    chain = it["chain"]
    target = it["target"]
    meta = CHAIN_META.get(chain) or {}
    weth = (meta.get("weth") or "").lower()
    try:
        w3 = connect_w3(chain)
    except Exception:
        return
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)").to_0x_hex()
    padded = "0x" + target.lower().replace("0x", "").rjust(64, "0")
    latest = w3.eth.block_number
    frm = max(0, latest - 8)
    logs = w3.eth.get_logs(
        {"fromBlock": frm, "toBlock": latest, "topics": [transfer_topic, None, padded]}
    )
    if it.get("copy_sell"):
        try:
            out_logs = w3.eth.get_logs(
                {"fromBlock": frm, "toBlock": latest, "topics": [transfer_topic, padded, None]}
            )
        except Exception:
            out_logs = []
    else:
        out_logs = []
    last = _copy_seen.get(it["id"], "")
    new_tokens = []
    for lg in logs:
        txh = lg["transactionHash"].hex() if hasattr(lg["transactionHash"], "hex") else str(lg["transactionHash"])
        token = lg["address"]
        if token.lower() in (weth,):
            continue
        key = txh + token
        if key == last or key in _copy_seen.get(f"{it['id']}_set", set()) if False else False:
            continue
        new_tokens.append((token, txh))
    sells = []
    for lg in out_logs:
        txh = lg["transactionHash"].hex() if hasattr(lg["transactionHash"], "hex") else str(lg["transactionHash"])
        token = lg["address"]
        if token.lower() in (weth,):
            continue
        sells.append((token, txh))
    if not new_tokens and not sells:
        return
    s = settings_of(it["user_id"], chain)
    amt = Decimal(str(it.get("buy_amount") or s["buy_amount"] or "0.05"))
    if new_tokens:
        token, txh = new_tokens[-1]
        _copy_seen[it["id"]] = txh + token
        if it.get("enabled"):
            res = await execute_buy(it["user_id"], chain, token, amt, multi=False)
            await notify(
                bot,
                it["user_id"],
                f"👫 <b>Copytrade buy</b> {chain}\nTracked <code>{target}</code>\nToken <code>{token}</code>\n" + "\n".join(res),
            )
        else:
            await notify(
                bot,
                it["user_id"],
                f"👁 <b>Copytrade (track only)</b> {chain}\n<code>{target}</code> received <code>{token}</code>\nPaste the CA to open Token Report.",
            )
    if sells and it.get("copy_sell") and it.get("enabled"):
        token, txh = sells[-1]
        res = await execute_sell(it["user_id"], chain, token, None, 100, multi=False)
        await notify(
            bot,
            it["user_id"],
            f"👫 <b>Copytrade sell</b> {chain}\n<code>{token}</code>\n" + "\n".join(res),
        )


async def _copy_sol(it: dict, bot) -> None:
    target = it["target"]
    try:
        sigs = await sol_rpc("getSignaturesForAddress", [target, {"limit": 5}])
    except Exception:
        return
    if not sigs:
        return
    newest = sigs[0].get("signature")
    prev = _copy_seen.get(it["id"])
    if not prev:
        _copy_seen[it["id"]] = newest
        return
    if newest == prev:
        return
    # walk new signatures until prev
    fresh = []
    for s in sigs:
        if s.get("signature") == prev:
            break
        fresh.append(s.get("signature"))
    _copy_seen[it["id"]] = newest
    if not fresh:
        return
    # fetch parsed tx for first new
    try:
        tx = await sol_rpc("getTransaction", [fresh[0], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        keys = ((((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
        mints = []
        meta = (tx or {}).get("meta") or {}
        bals = meta.get("postTokenBalances") or []
        for b in bals:
            mint = b.get("mint")
            if mint and mint != SOL_NATIVE:
                mints.append(mint)
        if not mints:
            return
        token = mints[-1]
        s = settings_of(it["user_id"], "SOL")
        amt = Decimal(str(it.get("buy_amount") or s["buy_amount"] or "0.05"))
        if it.get("enabled"):
            res = await execute_buy(it["user_id"], "SOL", token, amt, multi=False)
            await notify(bot, it["user_id"], f"👫 <b>Copytrade buy</b> SOL\n<code>{token}</code>\n" + "\n".join(res))
        else:
            await notify(bot, it["user_id"], f"👁 Copytrade SOL token <code>{token}</code>")
    except Exception as exc:
        log.debug("copy sol: %s", exc)


async def tick_copy(bot) -> None:
    items = db.list_copytrade_all()
    for it in items:
        try:
            kind = CHAINS.get(it["chain"], {}).get("kind")
            if kind == "sol":
                await _copy_sol(it, bot)
            elif kind == "evm":
                await asyncio.to_thread(lambda: None)
                await _copy_evm(it, bot)
        except Exception as exc:
            log.debug("copy %s: %s", it.get("id"), exc)


async def run(bot) -> None:
    log.info("worker started")
    n = 0
    while True:
        try:
            await tick_orders(bot)
            await tick_snipes(bot)
            if n % 2 == 0:
                await tick_copy(bot)
        except Exception as exc:
            log.warning("worker tick: %s", exc)
        n += 1
        await asyncio.sleep(8)
