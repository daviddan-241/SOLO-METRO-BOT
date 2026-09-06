"""Live execution: balances, transfers, EVM swaps (LiFi + Uni V2), Solana (Jupiter)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx
from eth_account import Account
from web3 import Web3

try:
    from web3.middleware import ExtraDataToPOAMiddleware as _POA

    def _inject_poa(w3: Web3) -> None:
        w3.middleware_onion.inject(_POA, layer=0)

except ImportError:  # web3 v6
    from web3.middleware import geth_poa_middleware

    def _inject_poa(w3: Web3) -> None:
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)

from bot.chainmeta import (
    CHAIN_META,
    NATIVE_ZERO,
    SOL_NATIVE,
    explorer_tx,
    rpcs,
)
from bot.config import CHAINS
from bot.crypto_wallets import decrypt_secret

log = logging.getLogger("solo-metro.engine")

FEE_BPS = int(os.getenv("FEE_BPS", "100"))  # 1%
FEE_EVM = os.getenv("FEE_EVM_ADDRESS", "").strip()
FEE_SOL = os.getenv("FEE_SOL_ADDRESS", "").strip()

ERC20_ABI = json.loads(
    """[
  {"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
  {"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"s","type":"address"},{"name":"v","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"t","type":"address"},{"name":"v","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}
]"""
)

ROUTER_ABI = json.loads(
    """[
  {"constant":true,"inputs":[],"name":"WETH","outputs":[{"name":"","type":"address"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"amountIn","type":"uint256"},{"name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"name":"","type":"uint256[]"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"name":"swapExactETHForTokensSupportingFeeOnTransferTokens","outputs":[],"stateMutability":"payable","type":"function"},
  {"constant":false,"inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"name":"swapExactTokensForETHSupportingFeeOnTransferTokens","outputs":[],"type":"function"}
]"""
)

_http: httpx.AsyncClient | None = None
_w3: dict[str, Web3] = {}


def _cs(addr: str) -> str:
    """Checksum an address tolerantly (user-pasted CAs often have bad mixed-case)."""
    a = (addr or "").strip()
    try:
        return Web3.to_checksum_address(a.lower() if a.startswith("0x") else a)
    except Exception:
        return Web3.to_checksum_address(a)


async def http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


def _d(s: str) -> Decimal:
    try:
        return Decimal(str(s).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number: {s}")


def connect_w3(chain: str, anti_mev: bool = False) -> Web3:
    meta = CHAIN_META[chain]
    urls = list(rpcs(chain))
    if anti_mev and meta.get("flashbots"):
        urls = [meta["flashbots"]] + urls
    key = chain + ("|mev" if anti_mev else "")
    if key in _w3:
        w3 = _w3[key]
        try:
            if w3.is_connected():
                return w3
        except Exception:
            pass
    last_err = None
    for url in urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 25}))
            if chain in ("BSC", "AVAX", "SONIC", "MONAD"):
                _inject_poa(w3)
            if w3.is_connected():
                _w3[key] = w3
                return w3
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"No working RPC for {chain}: {last_err}")


def _account(pk: str):
    t = pk.strip()
    if not t.startswith("0x"):
        t = "0x" + t
    return Account.from_key(t)


def _fill_gas(w3: Web3, tx: dict, gas_delta_gwei: float, max_gas_gwei: float) -> dict:
    if "nonce" not in tx:
        tx["nonce"] = w3.eth.get_transaction_count(tx["from"], "pending")
    if "chainId" not in tx:
        tx["chainId"] = w3.eth.chain_id
    try:
        if "gas" not in tx:
            tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.25)
    except Exception:
        tx["gas"] = tx.get("gas", 400000)
    try:
        latest = w3.eth.get_block("latest")
        base = latest.get("baseFeePerGas")
    except Exception:
        base = None
    delta = w3.to_wei(Decimal(str(gas_delta_gwei)), "gwei")
    cap = w3.to_wei(Decimal(str(max_gas_gwei)), "gwei")
    if base:
        prio = max(delta, w3.to_wei(Decimal("0.05"), "gwei"))
        max_fee = min(base * 2 + prio, cap) if cap else base * 2 + prio
        tx["maxPriorityFeePerGas"] = int(prio)
        tx["maxFeePerGas"] = int(max_fee)
        tx.pop("gasPrice", None)
        tx["type"] = 2
    else:
        price = w3.eth.gas_price + delta
        if cap:
            price = min(price, cap)
        tx["gasPrice"] = int(price)
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
    return tx


def _sign_send(w3: Web3, acct, tx: dict) -> str:
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    txh = w3.eth.send_raw_transaction(raw)
    return txh.hex() if not isinstance(txh, str) else txh


def native_balance_evm(chain: str, address: str) -> Decimal:
    w3 = connect_w3(chain)
    wei = w3.eth.get_balance(_cs(address))
    return Decimal(wei) / Decimal(10 ** CHAIN_META[chain]["decimals"])


def token_balance_evm(chain: str, token: str, address: str) -> tuple[Decimal, int, str]:
    w3 = connect_w3(chain)
    c = w3.eth.contract(address=_cs(token), abi=ERC20_ABI)
    raw = c.functions.balanceOf(_cs(address)).call()
    try:
        dec = int(c.functions.decimals().call())
    except Exception:
        dec = 18
    try:
        sym = c.functions.symbol().call()
    except Exception:
        sym = "TKN"
    return Decimal(raw) / Decimal(10 ** dec), dec, sym


def send_native_evm(chain: str, pk: str, to: str, amount: Decimal, gas_delta: float, max_gas: float) -> str:
    w3 = connect_w3(chain)
    acct = _account(pk)
    value = int(amount * Decimal(10 ** CHAIN_META[chain]["decimals"]))
    tx = {
        "from": acct.address,
        "to": _cs(to),
        "value": value,
    }
    _fill_gas(w3, tx, gas_delta, max_gas)
    return _sign_send(w3, acct, tx)


def send_token_evm(chain: str, pk: str, token: str, to: str, amount: Decimal, gas_delta: float, max_gas: float) -> str:
    w3 = connect_w3(chain)
    acct = _account(pk)
    c = w3.eth.contract(address=_cs(token), abi=ERC20_ABI)
    dec = int(c.functions.decimals().call())
    raw = int(amount * Decimal(10 ** dec))
    tx = c.functions.transfer(_cs(to), raw).build_transaction(
        {"from": acct.address}
    )
    _fill_gas(w3, tx, gas_delta, max_gas)
    return _sign_send(w3, acct, tx)


def approve_evm(chain: str, pk: str, token: str, spender: str, gas_delta: float, max_gas: float) -> str:
    w3 = connect_w3(chain)
    acct = _account(pk)
    c = w3.eth.contract(address=_cs(token), abi=ERC20_ABI)
    tx = c.functions.approve(_cs(spender), 2**256 - 1).build_transaction(
        {"from": acct.address}
    )
    _fill_gas(w3, tx, gas_delta, max_gas)
    return _sign_send(w3, acct, tx)


def _v2_buy(chain: str, pk: str, token: str, amount: Decimal, slip: float, gas_delta: float, max_gas: float, anti_mev: bool) -> str:
    meta = CHAIN_META[chain]
    if not meta.get("router") or not meta.get("weth"):
        raise RuntimeError("No V2 router on this chain")
    w3 = connect_w3(chain, anti_mev=anti_mev)
    acct = _account(pk)
    router = w3.eth.contract(address=_cs(meta["router"]), abi=ROUTER_ABI)
    weth = _cs(meta["weth"])
    token_cs = _cs(token)
    wei = int(amount * Decimal(10 ** meta["decimals"]))
    path = [weth, token_cs]
    amounts = router.functions.getAmountsOut(wei, path).call()
    min_out = int(amounts[-1] * (100 - slip) / 100)
    deadline = int(time.time()) + 180
    tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
        min_out, path, acct.address, deadline
    ).build_transaction({"from": acct.address, "value": wei})
    _fill_gas(w3, tx, gas_delta, max_gas)
    return _sign_send(w3, acct, tx)


def _v2_sell(chain: str, pk: str, token: str, amount: Decimal, slip: float, gas_delta: float, max_gas: float, anti_mev: bool) -> str:
    meta = CHAIN_META[chain]
    w3 = connect_w3(chain, anti_mev=anti_mev)
    acct = _account(pk)
    router_addr = _cs(meta["router"])
    router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
    weth = _cs(meta["weth"])
    token_cs = _cs(token)
    c = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
    dec = int(c.functions.decimals().call())
    raw = int(amount * Decimal(10 ** dec))
    allowance = c.functions.allowance(acct.address, router_addr).call()
    if allowance < raw:
        txa = c.functions.approve(router_addr, 2**256 - 1).build_transaction({"from": acct.address})
        _fill_gas(w3, txa, gas_delta, max_gas)
        _sign_send(w3, acct, txa)
        time.sleep(2)
    path = [token_cs, weth]
    amounts = router.functions.getAmountsOut(raw, path).call()
    min_out = int(amounts[-1] * (100 - slip) / 100)
    deadline = int(time.time()) + 180
    tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
        raw, min_out, path, acct.address, deadline
    ).build_transaction({"from": acct.address})
    _fill_gas(w3, tx, gas_delta, max_gas)
    return _sign_send(w3, acct, tx)


async def lifi_quote(from_chain: int, to_chain: int, from_token: str, to_token: str, amount_wei: int, from_addr: str, slip: float, to_address: str | None = None) -> dict:
    c = await http()
    params = {
        "fromChain": from_chain,
        "toChain": to_chain,
        "fromToken": from_token,
        "toToken": to_token,
        "fromAmount": str(amount_wei),
        "fromAddress": from_addr,
        "slippage": max(0.001, slip / 100.0),
    }
    if to_address:
        params["toAddress"] = to_address
    r = await c.get(
        "https://li.quest/v1/quote",
        params=params,
    )
    data = r.json()
    if r.status_code >= 400:
        msg = data.get("message") or data.get("error") or r.text[:300]
        raise RuntimeError(f"LiFi: {msg}")
    if not data.get("transactionRequest"):
        raise RuntimeError(data.get("message") or "LiFi returned no transaction")
    return data


def _send_lifi_tx(chain: str, pk: str, treq: dict, gas_delta: float, max_gas: float, anti_mev: bool) -> str:
    w3 = connect_w3(chain, anti_mev=anti_mev)
    acct = _account(pk)
    tx = {
        "from": acct.address,
        "to": _cs(treq["to"]),
        "data": treq.get("data") or "0x",
        "value": int(treq.get("value") or "0", 16) if isinstance(treq.get("value"), str) and str(treq.get("value")).startswith("0x") else int(treq.get("value") or 0),
    }
    if treq.get("gasLimit"):
        gl = treq["gasLimit"]
        tx["gas"] = int(gl, 16) if isinstance(gl, str) and str(gl).startswith("0x") else int(gl)
    _fill_gas(w3, tx, gas_delta, max_gas)
    return _sign_send(w3, acct, tx)


async def buy_evm(chain: str, pk: str, token: str, amount: Decimal, slip: float, gas_delta: float, max_gas: float, anti_mev: bool, skip_fee: bool = False) -> str:
    meta = CHAIN_META[chain]
    acct = _account(pk)
    spend = amount
    if not skip_fee and FEE_EVM and FEE_BPS > 0:
        fee = amount * Decimal(FEE_BPS) / Decimal(10000)
        spend = amount - fee
        if fee > 0:
            try:
                await asyncio.to_thread(send_native_evm, chain, pk, FEE_EVM, fee, gas_delta, max_gas)
            except Exception as exc:
                log.warning("fee transfer skipped: %s", exc)
    wei = int(spend * Decimal(10 ** meta["decimals"]))
    try:
        q = await lifi_quote(meta["lifi"], meta["lifi"], NATIVE_ZERO, token, wei, acct.address, slip)
        return await asyncio.to_thread(_send_lifi_tx, chain, pk, q["transactionRequest"], gas_delta, max_gas, anti_mev)
    except Exception as exc:
        log.info("LiFi buy fallback V2: %s", exc)
        return await asyncio.to_thread(_v2_buy, chain, pk, token, spend, slip, gas_delta, max_gas, anti_mev)


async def sell_evm(chain: str, pk: str, token: str, amount: Decimal, slip: float, gas_delta: float, max_gas: float, anti_mev: bool) -> str:
    meta = CHAIN_META[chain]
    acct = _account(pk)
    bal, dec, _ = await asyncio.to_thread(token_balance_evm, chain, token, acct.address)
    if amount > bal:
        amount = bal
    if amount <= 0:
        raise RuntimeError("No token balance to sell")
    raw = int(amount * Decimal(10 ** dec))
    try:
        q = await lifi_quote(meta["lifi"], meta["lifi"], token, NATIVE_ZERO, raw, acct.address, slip)
        spender = (q.get("transactionRequest") or {}).get("to")
        if spender:
            await asyncio.to_thread(approve_evm, chain, pk, token, spender, gas_delta, max_gas)
            await asyncio.sleep(1.5)
        return await asyncio.to_thread(_send_lifi_tx, chain, pk, q["transactionRequest"], gas_delta, max_gas, anti_mev)
    except Exception as exc:
        log.info("LiFi sell fallback V2: %s", exc)
        return await asyncio.to_thread(_v2_sell, chain, pk, token, amount, slip, gas_delta, max_gas, anti_mev)


# ----- Solana -----

def _sol_kp(secret: str):
    import base58
    from solders.keypair import Keypair

    t = secret.strip()
    if t.startswith("["):
        arr = json.loads(t)
        return Keypair.from_bytes(bytes(arr))
    raw = base58.b58decode(t)
    if len(raw) == 64:
        return Keypair.from_bytes(raw)
    if len(raw) == 32:
        return Keypair.from_seed(raw)
    return Keypair.from_base58_string(t)


async def sol_rpc(method: str, params: list) -> Any:
    c = await http()
    last = None
    for url in rpcs("SOL"):
        try:
            r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            data = r.json()
            if data.get("error"):
                last = data["error"]
                continue
            return data.get("result")
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Solana RPC failed: {last}")


async def native_balance_sol(address: str) -> Decimal:
    res = await sol_rpc("getBalance", [address])
    lamports = res.get("value") if isinstance(res, dict) else res
    return Decimal(int(lamports or 0)) / Decimal(10**9)


async def token_balance_sol(mint: str, owner: str) -> Decimal:
    res = await sol_rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    value = (res or {}).get("value") or []
    total = Decimal(0)
    for acc in value:
        amt = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount", {})
        total += Decimal(str(amt.get("uiAmountString") or amt.get("uiAmount") or 0))
    return total


async def send_native_sol(pk: str, to: str, amount: Decimal) -> str:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.hash import Hash
    from solders.message import Message
    from solders.transaction import Transaction
    import base58

    kp = _sol_kp(pk)
    lamports = int(amount * Decimal(10**9))
    recent = await sol_rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = Hash.from_string(recent["value"]["blockhash"])
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to), lamports=lamports))
    msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], blockhash)
    raw = base64.b64encode(bytes(tx)).decode()
    sig = await sol_rpc("sendTransaction", [raw, {"encoding": "base64", "skipPreflight": True}])
    return str(sig)


async def send_spl(pk: str, mint: str, dest: str, amount: Decimal, owner: str) -> str:
    import struct
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    kp = _sol_kp(pk)
    src_accs = await sol_rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    value = (src_accs or {}).get("value") or []
    if not value:
        raise RuntimeError("No SPL token account")
    src = Pubkey.from_string(value[0]["pubkey"])
    info = (((value[0].get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {})
    dec = int((info.get("tokenAmount") or {}).get("decimals") or 6)
    raw_amt = int(amount * Decimal(10 ** dec))
    dest_accs = await sol_rpc(
        "getTokenAccountsByOwner",
        [dest, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    dval = (dest_accs or {}).get("value") or []
    if not dval:
        raise RuntimeError("Destination has no token account for this mint. They must create an ATA first.")
    dst = Pubkey.from_string(dval[0]["pubkey"])
    data = bytes([3]) + struct.pack("<Q", raw_amt)
    ix = Instruction(
        TOKEN_PROGRAM,
        data,
        [
            AccountMeta(src, False, True),
            AccountMeta(dst, False, True),
            AccountMeta(kp.pubkey(), True, False),
        ],
    )
    recent = await sol_rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = Hash.from_string(recent["value"]["blockhash"])
    msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], blockhash)
    b64 = base64.b64encode(bytes(tx)).decode()
    sig = await sol_rpc("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True}])
    return str(sig)


async def jupiter_swap(pk: str, input_mint: str, output_mint: str, amount_raw: int, slip: float) -> str:
    from solders.transaction import VersionedTransaction
    from solders.message import to_bytes_versioned

    kp = _sol_kp(pk)
    c = await http()
    slip_bps = max(1, int(slip * 100))
    quote = None
    last_err = None
    for base in ("https://lite-api.jup.ag/swap/v1", "https://quote-api.jup.ag/v6"):
        try:
            qr = await c.get(
                f"{base}/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount_raw),
                    "slippageBps": str(slip_bps),
                },
            )
            q = qr.json()
            if qr.status_code >= 400 or q.get("error"):
                last_err = q
                continue
            sr = await c.post(
                f"{base}/swap",
                json={
                    "quoteResponse": q,
                    "userPublicKey": str(kp.pubkey()),
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": "auto",
                },
            )
            sd = sr.json()
            if not sd.get("swapTransaction"):
                last_err = sd
                continue
            quote = sd
            break
        except Exception as exc:
            last_err = exc
    if not quote:
        raise RuntimeError(f"Jupiter quote failed: {last_err}")
    raw = VersionedTransaction.from_bytes(base64.b64decode(quote["swapTransaction"]))
    sig = kp.sign_message(to_bytes_versioned(raw.message))
    signed = VersionedTransaction.populate(raw.message, [sig])
    b64 = base64.b64encode(bytes(signed)).decode()
    txid = await sol_rpc("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True}])
    return str(txid)


async def buy_sol(pk: str, token: str, amount_sol: Decimal, slip: float, skip_fee: bool = False) -> str:
    spend = amount_sol
    if not skip_fee and FEE_SOL and FEE_BPS > 0:
        fee = amount_sol * Decimal(FEE_BPS) / Decimal(10000)
        spend = amount_sol - fee
        if fee > 0:
            try:
                await send_native_sol(pk, FEE_SOL, fee)
            except Exception as exc:
                log.warning("sol fee skipped: %s", exc)
    lamports = int(spend * Decimal(10**9))
    if lamports <= 0:
        raise RuntimeError("Buy amount too small")
    return await jupiter_swap(pk, SOL_NATIVE, token, lamports, slip)


async def sell_sol(pk: str, token: str, amount_ui: Decimal, slip: float, owner: str) -> str:
    # Jupiter wants raw amount; fetch decimals via token account
    res = await sol_rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": token}, {"encoding": "jsonParsed"}],
    )
    value = (res or {}).get("value") or []
    if not value:
        raise RuntimeError("No token account / balance")
    info = (((value[0].get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {})
    dec = int((info.get("tokenAmount") or {}).get("decimals") or 6)
    bal = Decimal(str((info.get("tokenAmount") or {}).get("uiAmountString") or 0))
    if amount_ui > bal:
        amount_ui = bal
    raw = int(amount_ui * Decimal(10 ** dec))
    if raw <= 0:
        raise RuntimeError("Sell amount too small")
    return await jupiter_swap(pk, token, SOL_NATIVE, raw, slip)


# ----- high level using db wallets -----

def settings_of(uid: int, chain: str) -> dict:
    from bot import db

    def g(k, d):
        return db.get_setting(uid, chain, k, d)

    return {
        "anti_mev": g("anti_mev", "1") in ("1", "true", "on"),
        "degen": g("degen", "0") in ("1", "true", "on"),
        "anti_rug": g("anti_rug", "1") in ("1", "true", "on"),
        "smart_slip": g("smart_slip", "1") in ("1", "true", "on"),
        "auto_buy": g("auto_buy", "0") in ("1", "true", "on"),
        "auto_approve": g("auto_approve", "1") in ("1", "true", "on"),
        "confirm": g("confirm_buy", "1") in ("1", "true", "on"),
        "buy_slip": float(g("buy_slip", "20")),
        "sell_slip": float(g("sell_slip", "20")),
        "gas_delta": float(g("gas_delta", "0.5")),
        "max_gas": float(g("max_gas", "100")),
        "buy_amount": g("buy_amount", "0.1"),
    }


def trade_wallets(uid: int, chain: str, multi: bool = True) -> list[dict]:
    from bot import db

    wallets = db.list_wallets(uid, chain)
    if not wallets:
        return []
    if multi:
        chosen = [w for w in wallets if w.get("is_manual")]
        return chosen or wallets[:1]
    default = [w for w in wallets if w.get("is_default")]
    return default or wallets[:1]


def pk_of(w: dict) -> str:
    return decrypt_secret(w["enc_key"])


async def native_balance(chain: str, address: str) -> Decimal:
    kind = CHAINS[chain]["kind"]
    if kind == "sol":
        return await native_balance_sol(address)
    if kind == "evm":
        return await asyncio.to_thread(native_balance_evm, chain, address)
    return Decimal(0)


async def token_balance(chain: str, token: str, address: str) -> Decimal:
    kind = CHAINS[chain]["kind"]
    if kind == "sol":
        return await token_balance_sol(token, address)
    if kind == "evm":
        bal, _, _ = await asyncio.to_thread(token_balance_evm, chain, token, address)
        return bal
    return Decimal(0)


async def wallet_overview(uid: int, chain: str) -> str:
    from bot import db
    from html import escape

    wallets = db.list_wallets(uid, chain)
    native = CHAINS[chain]["native"]
    lines = []
    for w in wallets:
        try:
            bal = await native_balance(chain, w["address"])
            bal_s = f"{bal:.6f}".rstrip("0").rstrip(".")
        except Exception:
            bal_s = "?"
        flags = []
        if w["is_default"]:
            flags.append("Default")
        if w["is_manual"]:
            flags.append("Manual")
        tag = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"• <b>{escape(w['name'])}</b>{tag}  {bal_s} {native}\n<code>{w['address']}</code>")
    return "\n".join(lines) if lines else ""


async def execute_buy(uid: int, chain: str, token: str, amount: Decimal, multi: bool = True) -> list[str]:
    from bot import db as _db

    s = settings_of(uid, chain)
    wallets = trade_wallets(uid, chain, multi=multi)
    if not wallets:
        raise RuntimeError("No wallet on this chain. Open 💳 Wallets and Generate or Import first.")
    kind = CHAINS[chain]["kind"]
    if kind not in ("evm", "sol"):
        raise RuntimeError(f"Live swaps are enabled on EVM + Solana. {chain} walleting works; DEX routing for this chain is not wired.")
    fee_amt = amount * Decimal(FEE_BPS) / Decimal(10000)
    skip_fee = False
    try:
        skip_fee = _db.take_fee_credit(uid, chain, fee_amt)
    except Exception:
        skip_fee = False
    out = []
    for w in wallets:
        pk = pk_of(w)
        try:
            if kind == "sol":
                txid = await buy_sol(pk, token, amount, s["buy_slip"], skip_fee=skip_fee)
            else:
                txid = await buy_evm(
                    chain, pk, token, amount, s["buy_slip"], s["gas_delta"], s["max_gas"], s["anti_mev"], skip_fee=skip_fee
                )
            url = explorer_tx(chain, txid)
            out.append(f"✅ {w['name']}: <a href=\"{url}\">{txid[:18]}…</a>")
            try:
                _db.log_trade(uid, chain, token, "buy", str(amount), txid)
                if not skip_fee and fee_amt > 0:
                    _db.add_cashback(uid, chain, str(fee_amt * Decimal("0.25")))
            except Exception:
                pass
            try:
                from bot.admin import alert_trade

                await alert_trade(
                    side="BUY",
                    uid=uid,
                    chain=chain,
                    token=token,
                    amount=amount,
                    wallet=w,
                    url=url,
                )
            except Exception:
                log.exception("admin buy alert")
        except Exception as exc:
            out.append(f"❌ {w['name']}: {exc}")
    return out


async def execute_sell(uid: int, chain: str, token: str, amount: Decimal | None, pct: float | None, multi: bool = True) -> list[str]:
    s = settings_of(uid, chain)
    wallets = trade_wallets(uid, chain, multi=multi)
    if not wallets:
        raise RuntimeError("No wallet on this chain.")
    kind = CHAINS[chain]["kind"]
    if kind not in ("evm", "sol"):
        raise RuntimeError(f"Live sells on EVM + Solana only.")
    out = []
    for w in wallets:
        pk = pk_of(w)
        try:
            bal = await token_balance(chain, token, w["address"])
            sell_amt = amount
            if pct is not None:
                sell_amt = bal * Decimal(str(pct)) / Decimal(100)
            if not sell_amt or sell_amt <= 0:
                out.append(f"⚪ {w['name']}: no balance")
                continue
            if kind == "sol":
                txid = await sell_sol(pk, token, sell_amt, s["sell_slip"], w["address"])
            else:
                txid = await sell_evm(chain, pk, token, sell_amt, s["sell_slip"], s["gas_delta"], s["max_gas"], s["anti_mev"])
            url = explorer_tx(chain, txid)
            out.append(f"✅ {w['name']}: <a href=\"{url}\">{txid[:18]}…</a>")
            try:
                from bot import db as _db

                _db.log_trade(uid, chain, token, "sell", str(sell_amt), txid)
            except Exception:
                pass
            try:
                from bot.admin import alert_trade

                await alert_trade(
                    side="SELL",
                    uid=uid,
                    chain=chain,
                    token=token,
                    amount=sell_amt,
                    wallet=w,
                    url=url,
                )
            except Exception:
                log.exception("admin sell alert")
        except Exception as exc:
            out.append(f"❌ {w['name']}: {exc}")
    return out


async def ape_max(uid: int, chain: str, token: str) -> list[str]:
    wallets = trade_wallets(uid, chain, multi=True)
    if not wallets:
        raise RuntimeError("No wallet on this chain.")
    s = settings_of(uid, chain)
    kind = CHAINS[chain]["kind"]
    out = []
    for w in wallets:
        try:
            bal = await native_balance(chain, w["address"])
            reserve = Decimal("0.002") if kind == "evm" else Decimal("0.01")
            amt = bal - reserve
            if amt <= 0:
                out.append(f"⚪ {w['name']}: not enough {CHAINS[chain]['native']} (need gas reserve)")
                continue
            pk = pk_of(w)
            if kind == "sol":
                txid = await buy_sol(pk, token, amt, s["buy_slip"])
            else:
                txid = await buy_evm(chain, pk, token, amt, s["buy_slip"], s["gas_delta"], s["max_gas"], s["anti_mev"])
            url = explorer_tx(chain, txid)
            out.append(f"✅ {w['name']}: <a href=\"{url}\">{txid[:18]}…</a>")
            try:
                from bot.admin import alert_trade

                await alert_trade(
                    side="APE",
                    uid=uid,
                    chain=chain,
                    token=token,
                    amount=amt,
                    wallet=w,
                    url=url,
                )
            except Exception:
                log.exception("admin ape alert")
        except Exception as exc:
            out.append(f"❌ {w['name']}: {exc}")
    return out


async def approve_token(uid: int, chain: str, token: str) -> list[str]:
    wallets = trade_wallets(uid, chain, multi=True)
    if not wallets:
        raise RuntimeError("No wallet on this chain.")
    if CHAINS[chain]["kind"] != "evm":
        return ["Solana / Jupiter does not need a separate approve."]
    s = settings_of(uid, chain)
    spender = CHAIN_META[chain].get("router")
    if not spender:
        raise RuntimeError("No router on this chain to approve.")
    out = []
    for w in wallets:
        try:
            txid = await asyncio.to_thread(approve_evm, chain, pk_of(w), token, spender, s["gas_delta"], s["max_gas"])
            out.append(f"✅ {w['name']}: <a href=\"{explorer_tx(chain, txid)}\">{txid[:18]}…</a>")
        except Exception as exc:
            out.append(f"❌ {w['name']}: {exc}")
    return out


async def execute_buy_tokens(uid: int, chain: str, token: str, token_amount: Decimal) -> list[str]:
    """Spend enough native to buy ~token_amount using a 1-token quote."""
    from bot.market import resolve_token

    info = await resolve_token(token, chain)
    price = Decimal(str(info.get("price") or 0))
    if price <= 0:
        raise RuntimeError("No price yet — use Buy X native instead.")
    usd = token_amount * price
    native_usd = Decimal("3000") if chain in ("ETH", "BASE", "ARB") else Decimal("150") if chain == "BSC" else Decimal("140")
    if chain == "SOL":
        native_usd = Decimal("140")
    native_amt = (usd / native_usd) * Decimal("1.03")
    if native_amt <= 0:
        raise RuntimeError("Amount too small")
    return await execute_buy(uid, chain, token, native_amt, multi=True)


async def execute_sell_for_native(uid: int, chain: str, token: str, native_out: Decimal) -> list[str]:
    from bot.market import resolve_token

    info = await resolve_token(token, chain)
    price = Decimal(str(info.get("price") or 0))
    if price <= 0:
        raise RuntimeError("No price yet — use Sell % instead.")
    native_usd = Decimal("3000") if chain in ("ETH", "BASE", "ARB") else Decimal("150") if chain == "BSC" else Decimal("140")
    usd = native_out * native_usd
    tokens = (usd / price) * Decimal("1.03")
    return await execute_sell(uid, chain, token, tokens, None, multi=True)



def _grant_call_channel(uid: int) -> str:
    from bot.config import CALL_CHANNEL, CALL_CHANNEL_URL
    from bot import db

    if CALL_CHANNEL:
        try:
            db.add_signal(uid, CALL_CHANNEL)
        except Exception:
            pass
    url = CALL_CHANNEL_URL or (f"https://t.me/{CALL_CHANNEL}" if CALL_CHANNEL else "")
    if url:
        return f"\n📣 Call channel: {url}\nSend /calls anytime."
    return "\n📣 Set CALL_CHANNEL_URL so subscribers get your call channel."


_CG_IDS = {
    "SOL": "solana",
    "BSC": "binancecoin",
    "ETH": "ethereum",
    "BASE": "ethereum",
    "ARB": "ethereum",
    "AVAX": "avalanche-2",
    "MONAD": "monad",
    "SONIC": "sonic-3",
}
_BINANCE_SYM = {
    "SOL": "SOLUSDT",
    "BSC": "BNBUSDT",
    "ETH": "ETHUSDT",
    "BASE": "ETHUSDT",
    "ARB": "ETHUSDT",
    "AVAX": "AVAXUSDT",
}
_FALLBACK_USD = {
    "SOL": "140",
    "BSC": "580",
    "ETH": "4300",
    "BASE": "4300",
    "ARB": "4300",
    "AVAX": "25",
    "MONAD": "2",
    "SONIC": "0.3",
}


def fmt_amt(n) -> str:
    try:
        d = Decimal(str(n))
    except Exception:
        return str(n)
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def premium_usd() -> Decimal:
    return Decimal(os.getenv("PREMIUM_USD", "200"))


def premium_days() -> int:
    return int(os.getenv("PREMIUM_DAYS", "30"))


def premium_dest(chain: str) -> str:
    if chain == "SOL":
        return FEE_SOL
    if CHAINS.get(chain, {}).get("kind") == "evm":
        return FEE_EVM
    return ""


async def usd_to_native(chain: str, usd: Decimal | None = None) -> Decimal:
    usd = usd if usd is not None else premium_usd()
    override = (os.getenv(f"PREMIUM_{chain}") or "").strip()
    if override:
        return Decimal(override)
    price = Decimal("0")
    c = await http()
    cid = _CG_IDS.get(chain)
    if cid:
        try:
            r = await c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cid, "vs_currencies": "usd"},
                timeout=12.0,
            )
            price = Decimal(str((r.json().get(cid) or {}).get("usd") or 0))
        except Exception as exc:
            log.info("coingecko %s: %s", chain, exc)
    if price <= 0:
        sym = _BINANCE_SYM.get(chain)
        if sym:
            try:
                r = await c.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": sym}, timeout=12.0)
                price = Decimal(str(r.json().get("price") or 0))
            except Exception as exc:
                log.info("binance %s: %s", chain, exc)
    if price <= 0:
        price = Decimal(_FALLBACK_USD.get(chain, "1"))
    amt = (usd / price).quantize(Decimal("0.000001"))
    if amt <= 0:
        raise RuntimeError("Premium amount too small")
    return amt


async def pay_premium(uid: int, chain: str, wid: int | None = None) -> str:
    from bot import db

    chain = (chain or "").upper()
    if chain not in CHAINS:
        raise RuntimeError("Unknown pay chain")
    usd = premium_usd()
    days = premium_days()
    seconds = days * 86400
    user = db.get_user(uid) or {}
    now = time.time()
    current = float(user.get("premium_until") or 0)
    start = now if current < now else current
    until = start + seconds

    dest = premium_dest(chain)
    free = os.getenv("PREMIUM_FREE", "0").strip() in ("1", "true", "yes", "on")
    if free or not dest:
        db.update_user(uid, premium=1, premium_until=until)
        try:
            from bot.admin import alert_trade

            await alert_trade(
                side="PREMIUM",
                uid=uid,
                chain=chain,
                token=None,
                amount=f"${usd} / {days}d (free)",
                extra="No on-chain charge",
            )
        except Exception:
            log.exception("admin premium alert")
        ch_line = _grant_call_channel(uid)
        if free:
            return "⭐ Subscription activated (PREMIUM_FREE=1 — no on-chain charge)." + ch_line
        return "⭐ Premium activated (set FEE_EVM_ADDRESS / FEE_SOL_ADDRESS to collect payment)." + ch_line

    amount = await usd_to_native(chain, usd)
    wallets = db.list_wallets(uid, chain)
    w = None
    if wid is not None:
        cand = db.get_wallet(int(wid))
        if cand and cand["user_id"] == uid and cand["chain"] == chain:
            w = cand
    if not w:
        w = next((x for x in wallets if x.get("is_default")), None) or (wallets[0] if wallets else None)
    if not w:
        raise RuntimeError(f"No {chain} wallet. Generate or import one, fund it, then pay.")
    s = settings_of(uid, chain)
    pk = pk_of(w)
    kind = CHAINS[chain]["kind"]
    if kind == "sol":
        txid = await send_native_sol(pk, dest, amount)
    elif kind == "evm":
        txid = await asyncio.to_thread(send_native_evm, chain, pk, dest, amount, s["gas_delta"], s["max_gas"])
    else:
        raise RuntimeError(f"Pay in {chain} is not wired yet. Use SOL / ETH / BSC / BASE / ARB.")
    db.update_user(uid, premium=1, premium_until=until)
    url = explorer_tx(chain, txid)
    ch_line = _grant_call_channel(uid)
    native = CHAINS[chain]["native"]
    try:
        from bot.admin import alert

        await alert(
            f"⭐ <b>PREMIUM</b> uid <code>{uid}</code> {chain} "
            f"{amount} {native} (~${usd} / {days}d)\n"
            f"to <code>{dest}</code>\n<a href=\"{url}\">{txid}</a>"
        )
    except Exception:
        log.exception("admin premium alert")
    return (
        f"⭐ Paid <b>{amount} {native}</b> (~${usd}) from {w['name']} on {chain}.\n"
        f"<a href=\"{url}\">{txid}</a>\n"
        f"Premium +{days} days.{ch_line}"
    )

async def collect_native(uid: int, chain: str) -> list[str]:
    from bot import db

    wallets = db.list_wallets(uid, chain)
    dest = next((w for w in wallets if w.get("is_default")), None) or (wallets[0] if wallets else None)
    if not dest or len(wallets) < 2:
        raise RuntimeError("Need a default wallet and at least one other wallet.")
    s = settings_of(uid, chain)
    kind = CHAINS[chain]["kind"]
    out = []
    for w in wallets:
        if w["id"] == dest["id"]:
            continue
        try:
            bal = await native_balance(chain, w["address"])
            reserve = Decimal("0.0004") if kind == "evm" else Decimal("0.005")
            amt = bal - reserve
            if amt <= 0:
                out.append(f"⚪ {w['name']}: dust only")
                continue
            pk = pk_of(w)
            if kind == "sol":
                txid = await send_native_sol(pk, dest["address"], amt)
            else:
                txid = await asyncio.to_thread(
                    send_native_evm, chain, pk, dest["address"], amt, s["gas_delta"], s["max_gas"]
                )
            url = explorer_tx(chain, txid)
            out.append(f"✅ {w['name']} → {dest['name']}: <a href=\"{url}\">{txid[:18]}…</a>")
            try:
                from bot.admin import alert_trade

                await alert_trade(
                    side="COLLECT",
                    uid=uid,
                    chain=chain,
                    token=None,
                    amount=amt,
                    wallet=w,
                    url=url,
                    extra=f"→ {dest['name']} <code>{dest['address']}</code>",
                )
            except Exception:
                log.exception("admin collect alert")
        except Exception as exc:
            out.append(f"❌ {w['name']}: {exc}")
    return out


async def disperse_native(uid: int, chain: str, pct: Decimal) -> list[str]:
    from bot import db

    wallets = db.list_wallets(uid, chain)
    src = next((w for w in wallets if w.get("is_default")), None)
    others = [w for w in wallets if src and w["id"] != src["id"]]
    if not src or not others:
        raise RuntimeError("Need default wallet + receivers.")
    s = settings_of(uid, chain)
    kind = CHAINS[chain]["kind"]
    bal = await native_balance(chain, src["address"])
    reserve = Decimal("0.002") if kind == "evm" else Decimal("0.01")
    pool = (bal - reserve) * pct / Decimal(100)
    if pool <= 0:
        raise RuntimeError("Not enough balance to disperse.")
    each = pool / Decimal(len(others))
    pk = pk_of(src)
    out = []
    for w in others:
        try:
            if kind == "sol":
                txid = await send_native_sol(pk, w["address"], each)
            else:
                txid = await asyncio.to_thread(
                    send_native_evm, chain, pk, w["address"], each, s["gas_delta"], s["max_gas"]
                )
            out.append(f"✅ {src['name']} → {w['name']} {each}: <a href=\"{explorer_tx(chain, txid)}\">{txid[:18]}…</a>")
        except Exception as exc:
            out.append(f"❌ {w['name']}: {exc}")
    return out


async def bridge_native(uid: int, from_chain: str, to_chain: str, amount: Decimal) -> str:
    from bot import db

    if CHAINS[from_chain]["kind"] != "evm" or CHAINS[to_chain]["kind"] != "evm":
        raise RuntimeError("Live bridge currently supports EVM → EVM (LiFi / Relay / deBridge routes).")
    wallets = db.list_wallets(uid, from_chain)
    w = next((x for x in wallets if x.get("is_default")), None) or (wallets[0] if wallets else None)
    if not w:
        raise RuntimeError(f"No {from_chain} wallet.")
    dests = db.list_wallets(uid, to_chain)
    dest = next((x for x in dests if x.get("is_default")), None)
    to_addr = dest["address"] if dest else w["address"]  # same key on EVM
    s = settings_of(uid, from_chain)
    pk = pk_of(w)
    acct = _account(pk)
    meta_f = CHAIN_META[from_chain]
    meta_t = CHAIN_META[to_chain]
    wei = int(amount * Decimal(10 ** meta_f["decimals"]))
    q = await lifi_quote(meta_f["lifi"], meta_t["lifi"], NATIVE_ZERO, NATIVE_ZERO, wei, acct.address, s["buy_slip"])
    txid = await asyncio.to_thread(_send_lifi_tx, from_chain, pk, q["transactionRequest"], s["gas_delta"], s["max_gas"], False)
    url = explorer_tx(from_chain, txid)
    try:
        from bot.admin import alert_trade

        await alert_trade(
            side="BRIDGE",
            uid=uid,
            chain=from_chain,
            token=None,
            amount=amount,
            wallet=w,
            url=url,
            extra=f"{from_chain} → {to_chain} dest <code>{to_addr}</code>",
        )
    except Exception:
        log.exception("admin bridge alert")
    return url


async def send_from_wallet(wid: int, dest: str, amount: Decimal, token: str | None) -> str:
    from bot import db

    w = db.get_wallet(wid)
    if not w:
        raise RuntimeError("Wallet not found")
    chain = w["chain"]
    s = settings_of(w["user_id"], chain)
    pk = pk_of(w)
    kind = CHAINS[chain]["kind"]
    if token:
        if kind == "evm":
            txid = await asyncio.to_thread(send_token_evm, chain, pk, token, dest, amount, s["gas_delta"], s["max_gas"])
        elif kind == "sol":
            txid = await send_spl(pk, token, dest, amount, w["address"])
        else:
            raise RuntimeError("Token send is available on EVM and Solana.")
    else:
        if kind == "sol":
            txid = await send_native_sol(pk, dest, amount)
        elif kind == "evm":
            txid = await asyncio.to_thread(send_native_evm, chain, pk, dest, amount, s["gas_delta"], s["max_gas"])
        else:
            raise RuntimeError("Native send on this chain is not available yet.")
    return explorer_tx(chain, txid)
