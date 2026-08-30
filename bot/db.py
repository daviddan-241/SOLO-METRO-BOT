import json
import sqlite3
import threading
import time
import secrets
from contextlib import contextmanager
from typing import Any, Optional

from bot.config import DB_PATH, CHAIN_ORDER, MAX_WALLETS_FREE, MAX_WALLETS_PREMIUM

_lock = threading.Lock()


@contextmanager
def connect():
    with _lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                verified INTEGER DEFAULT 0,
                language TEXT DEFAULT 'en',
                premium INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                captcha_text TEXT,
                captcha_attempts INTEGER DEFAULT 0,
                captcha_lock_until REAL DEFAULT 0,
                created_at REAL,
                tos_accepted INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                data TEXT
            );

            CREATE TABLE IF NOT EXISTS chain_prefs (
                user_id INTEGER,
                chain TEXT,
                enabled INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, chain)
            );

            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chain TEXT,
                name TEXT,
                address TEXT,
                enc_key TEXT,
                is_manual INTEGER DEFAULT 0,
                is_default INTEGER DEFAULT 0,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER,
                chain TEXT,
                key TEXT,
                value TEXT,
                PRIMARY KEY (user_id, chain, key)
            );

            CREATE TABLE IF NOT EXISTS copytrade (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chain TEXT,
                label TEXT,
                target TEXT,
                buy_amount TEXT,
                enabled INTEGER DEFAULT 1,
                copy_sell INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS autosnipe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chain TEXT,
                token TEXT,
                amount TEXT,
                enabled INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chain TEXT,
                token TEXT,
                side TEXT,
                trigger_type TEXT,
                trigger_value TEXT,
                amount TEXT,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chain TEXT,
                token TEXT,
                created_at REAL
            );

            CREATE TABLE IF NOT EXISTS referrals (
                code TEXT PRIMARY KEY,
                owner_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                source TEXT,
                chain TEXT,
                enabled INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chain TEXT,
                token TEXT,
                side TEXT,
                amount TEXT,
                txid TEXT,
                created_at REAL
            );
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(monitors)").fetchall()}
        if "qty" not in cols:
            con.execute("ALTER TABLE monitors ADD COLUMN qty TEXT")
        if "cost" not in cols:
            con.execute("ALTER TABLE monitors ADD COLUMN cost TEXT")


def ensure_user(user_id: int, username: str | None, first_name: str | None) -> dict:
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            con.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (username, first_name, user_id),
            )
            return dict(con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())
        code = secrets.token_hex(3).upper()
        while con.execute("SELECT 1 FROM users WHERE referral_code=?", (code,)).fetchone():
            code = secrets.token_hex(3).upper()
        now = time.time()
        con.execute(
            """INSERT INTO users (user_id, username, first_name, referral_code, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, first_name, code, now),
        )
        for chain in CHAIN_ORDER:
            con.execute(
                "INSERT OR IGNORE INTO chain_prefs (user_id, chain, enabled) VALUES (?, ?, 1)",
                (user_id, chain),
            )
        return dict(con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())


def get_user(user_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_user(user_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [user_id]
    with connect() as con:
        con.execute(f"UPDATE users SET {keys} WHERE user_id=?", vals)


def set_state(user_id: int, state: str | None, data: dict | None = None) -> None:
    with connect() as con:
        if not state:
            con.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
            return
        con.execute(
            "INSERT INTO user_state(user_id, state, data) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, data=excluded.data",
            (user_id, state, json.dumps(data or {})),
        )


def get_state(user_id: int) -> tuple[str | None, dict]:
    with connect() as con:
        row = con.execute(
            "SELECT state, data FROM user_state WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return None, {}
        try:
            payload = json.loads(row["data"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return row["state"], payload


def enabled_chains(user_id: int) -> list[str]:
    with connect() as con:
        rows = con.execute(
            "SELECT chain, enabled FROM chain_prefs WHERE user_id=?", (user_id,)
        ).fetchall()
        if not rows:
            return list(CHAIN_ORDER)
        flags = {r["chain"]: r["enabled"] for r in rows}
        return [c for c in CHAIN_ORDER if flags.get(c, 1)]


def all_chain_flags(user_id: int) -> dict[str, int]:
    with connect() as con:
        rows = con.execute(
            "SELECT chain, enabled FROM chain_prefs WHERE user_id=?", (user_id,)
        ).fetchall()
        flags = {c: 1 for c in CHAIN_ORDER}
        for r in rows:
            flags[r["chain"]] = r["enabled"]
        return flags


def toggle_chain(user_id: int, chain: str) -> int:
    flags = all_chain_flags(user_id)
    new_val = 0 if flags.get(chain, 1) else 1
    with connect() as con:
        con.execute(
            "INSERT INTO chain_prefs(user_id, chain, enabled) VALUES(?,?,?) "
            "ON CONFLICT(user_id, chain) DO UPDATE SET enabled=excluded.enabled",
            (user_id, chain, new_val),
        )
    return new_val


def list_wallets(user_id: int, chain: str) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM wallets WHERE user_id=? AND chain=? ORDER BY id",
            (user_id, chain),
        ).fetchall()
        return [dict(r) for r in rows]


def wallet_count(user_id: int, chain: str) -> int:
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM wallets WHERE user_id=? AND chain=?",
            (user_id, chain),
        ).fetchone()
        return int(row["c"])


def max_wallets(user: dict) -> int:
    return MAX_WALLETS_PREMIUM if user.get("premium") else MAX_WALLETS_FREE


def add_wallet(
    user_id: int,
    chain: str,
    name: str,
    address: str,
    enc_key: str,
) -> dict:
    existing = list_wallets(user_id, chain)
    is_first = len(existing) == 0
    with connect() as con:
        cur = con.execute(
            """INSERT INTO wallets (user_id, chain, name, address, enc_key, is_manual, is_default, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                chain,
                name,
                address,
                enc_key,
                1 if is_first else 0,
                1 if is_first else 0,
                time.time(),
            ),
        )
        wid = cur.lastrowid
        row = con.execute("SELECT * FROM wallets WHERE id=?", (wid,)).fetchone()
        return dict(row)


def get_wallet(wallet_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM wallets WHERE id=?", (wallet_id,)).fetchone()
        return dict(row) if row else None


def update_wallet(wallet_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [wallet_id]
    with connect() as con:
        con.execute(f"UPDATE wallets SET {keys} WHERE id=?", vals)


def delete_wallet(wallet_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM wallets WHERE id=?", (wallet_id,))


def set_default_wallet(user_id: int, chain: str, wallet_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE wallets SET is_default=0 WHERE user_id=? AND chain=?",
            (user_id, chain),
        )
        con.execute("UPDATE wallets SET is_default=1 WHERE id=?", (wallet_id,))


def get_setting(user_id: int, chain: str, key: str, default: str) -> str:
    with connect() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE user_id=? AND chain=? AND key=?",
            (user_id, chain, key),
        ).fetchone()
        return row["value"] if row else default


def set_setting(user_id: int, chain: str, key: str, value: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO settings(user_id, chain, key, value) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, chain, key) DO UPDATE SET value=excluded.value",
            (user_id, chain, key, value),
        )


def toggle_setting(user_id: int, chain: str, key: str, default: str = "0") -> str:
    cur = get_setting(user_id, chain, key, default)
    new = "0" if cur in ("1", "true", "on") else "1"
    set_setting(user_id, chain, key, new)
    return new


def list_copytrade(user_id: int, chain: str | None = None) -> list[dict]:
    with connect() as con:
        if chain:
            rows = con.execute(
                "SELECT * FROM copytrade WHERE user_id=? AND chain=? ORDER BY id",
                (user_id, chain),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM copytrade WHERE user_id=? ORDER BY id", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def add_copytrade(user_id: int, chain: str, label: str, target: str, buy_amount: str) -> dict:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO copytrade (user_id, chain, label, target, buy_amount, enabled, copy_sell)
               VALUES (?, ?, ?, ?, ?, 1, 1)""",
            (user_id, chain, label, target, buy_amount),
        )
        row = con.execute("SELECT * FROM copytrade WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def delete_copytrade(item_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM copytrade WHERE id=?", (item_id,))


def update_copytrade(item_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    with connect() as con:
        con.execute(f"UPDATE copytrade SET {keys} WHERE id=?", vals)


def get_copytrade(item_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM copytrade WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None


def list_snipes(user_id: int, chain: str | None = None) -> list[dict]:
    with connect() as con:
        if chain:
            rows = con.execute(
                "SELECT * FROM autosnipe WHERE user_id=? AND chain=? ORDER BY id",
                (user_id, chain),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM autosnipe WHERE user_id=? ORDER BY id", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def add_snipe(user_id: int, chain: str, token: str, amount: str) -> dict:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO autosnipe (user_id, chain, token, amount, enabled) VALUES (?,?,?,?,1)",
            (user_id, chain, token, amount),
        )
        row = con.execute("SELECT * FROM autosnipe WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def delete_snipe(item_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM autosnipe WHERE id=?", (item_id,))


def get_snipe(item_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM autosnipe WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None


def update_snipe(item_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    with connect() as con:
        con.execute(f"UPDATE autosnipe SET {keys} WHERE id=?", vals)


def list_orders(user_id: int, status: str = "active") -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND status=? ORDER BY id",
            (user_id, status),
        ).fetchall()
        return [dict(r) for r in rows]


def add_order(user_id: int, chain: str, token: str, side: str, trigger_type: str, trigger_value: str, amount: str) -> dict:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO orders (user_id, chain, token, side, trigger_type, trigger_value, amount, status)
               VALUES (?,?,?,?,?,?,?,'active')""",
            (user_id, chain, token, side, trigger_type, trigger_value, amount),
        )
        row = con.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def delete_order(item_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM orders WHERE id=?", (item_id,))


def list_monitors(user_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM monitors WHERE user_id=? ORDER BY id DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_monitor(user_id: int, chain: str, token: str) -> dict:
    with connect() as con:
        existing = con.execute(
            "SELECT * FROM monitors WHERE user_id=? AND chain=? AND token=?",
            (user_id, chain, token),
        ).fetchone()
        if existing:
            return dict(existing)
        cur = con.execute(
            "INSERT INTO monitors (user_id, chain, token, created_at) VALUES (?,?,?,?)",
            (user_id, chain, token, time.time()),
        )
        row = con.execute("SELECT * FROM monitors WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def clear_monitors(user_id: int) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM monitors WHERE user_id=?", (user_id,))
        return cur.rowcount


def delete_monitor(item_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM monitors WHERE id=?", (item_id,))


def list_copytrade_all() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM copytrade").fetchall()
        return [dict(r) for r in rows]


def update_order_status(item_id: int, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE orders SET status=? WHERE id=?", (status, item_id))


def add_signal(user_id: int, source: str, chain: str = "ETH") -> dict:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO signals (user_id, source, chain, enabled) VALUES (?,?,?,1)",
            (user_id, source, chain),
        )
        row = con.execute("SELECT * FROM signals WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def list_signals(user_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM signals WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [dict(r) for r in rows]


def log_trade(user_id: int, chain: str, token: str, side: str, amount: str, txid: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO trades (user_id, chain, token, side, amount, txid, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, chain, token, side, amount, txid, time.time()),
        )


def referral_count(user_id: int) -> int:
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referred_by=?", (user_id,)
        ).fetchone()
        return int(row["c"])
