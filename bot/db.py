import json
import sqlite3
import threading
import time
import secrets
from contextlib import contextmanager
from typing import Any, Optional

from bot.config import (
    CHAIN_ORDER,
    DB_PATH,
    MAX_COPY_FREE,
    MAX_COPY_PREMIUM,
    MAX_MONITOR_FREE,
    MAX_MONITOR_PREMIUM,
    MAX_ORDER_FREE,
    MAX_ORDER_PREMIUM,
    MAX_SNIPE_FREE,
    MAX_SNIPE_PREMIUM,
    MAX_WALLETS_FREE,
    MAX_WALLETS_PREMIUM,
    TRENDING_FREE,
    TRENDING_PREMIUM,
)

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# DUAL ENGINE: PostgreSQL (free Neon/Supabase via DATABASE_URL) or SQLite.
# Postgres makes the bot fully persistent across Render restarts/redeploys;
# SQLite remains the zero-config fallback.
# ---------------------------------------------------------------------------
import os as _os

DATABASE_URL = (_os.getenv("DATABASE_URL") or "").strip()
ENGINE = "postgres" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"

_pg_pool = None


def _qmark_to_psql(sql: str) -> str:
    """SQLite '?' placeholders -> '%s' for psycopg; literal '%' escaped.
    Quote-aware so '?' inside string literals is left alone."""
    out = []
    i = 0
    in_str = False
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and (i + 1 >= len(sql) or sql[i + 1] != "'"):
            in_str = not in_str
            out.append(ch)
        elif ch == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
            out.append("''")
            i += 1
        elif ch == "%":
            # psycopg %-formats the WHOLE query (literals included) when
            # parameters are present — every bare % must be doubled
            out.append("%%")
        elif not in_str and ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _pg_open_pool():
    """(Re)create the connection pool. Neon free tier drops idle connections —
    the pool health-checks and reconnects automatically."""
    global _pg_pool
    import logging as _logging

    log = _logging.getLogger("solo-metro.db")
    from psycopg_pool import ConnectionPool

    last = None
    for attempt in range(5):
        try:
            kwargs = dict(
                min_size=1,
                max_size=8,
                max_idle=120,
                max_lifetime=1800,
                timeout=25,
                open=True,
            )
            try:
                kwargs["check"] = ConnectionPool.check_connection
            except Exception:
                pass
            _pg_pool = ConnectionPool(DATABASE_URL, **kwargs)
            log.info("Postgres pool ready")
            return
        except Exception as exc:
            last = exc
            log.warning("Postgres pool attempt %d failed: %s", attempt + 1, exc)
            time.sleep(8)
    raise RuntimeError(
        f"Could not connect to DATABASE_URL ({str(last)[:200]}). "
        "Create a free Postgres at neon.tech and set DATABASE_URL."
    )


class _PgCursor:
    """psycopg cursor shim exposing sqlite-style .lastrowid (via RETURNING)."""

    def __init__(self, cur, last_id):
        self._cur = cur
        self.lastrowid = last_id

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConn:
    """psycopg connection shim: sqlite-style execute(?, params)."""

    def __init__(self, real):
        self._real = real
        try:
            from psycopg.rows import dict_row

            real.row_factory = dict_row
        except Exception:
            pass

    def execute(self, sql, params=()):
        sql2 = _qmark_to_psql(sql)
        p = tuple(params) if params else None
        stripped = sql2.strip().lstrip("(").strip().upper()
        if stripped.startswith("INSERT") and "RETURNING" not in sql2.upper():
            cur = self._real.execute(sql2 + " RETURNING id", p)
            try:
                row = cur.fetchone()
                last_id = row["id"] if row else None
            except Exception:
                last_id = None
            return _PgCursor(cur, last_id)
        return _PgCursor(self._real.execute(sql2, p), None)

    def executescript(self, script: str):
        for stmt in script.split(";"):
            if stmt.strip():
                self._real.execute(stmt)
        return None

    def commit(self):
        try:
            self._real.commit()
        except Exception:
            pass

    def rollback(self):
        try:
            self._real.rollback()
        except Exception:
            pass


def pg_health() -> tuple[bool, str]:
    """Simple liveness + counts for boot self-check on Postgres."""
    try:
        with connect() as con:
            u = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            w = con.execute("SELECT COUNT(*) AS n FROM wallets").fetchone()
            return True, f"users={u['n']} wallets={w['n']}"
    except Exception as exc:
        return False, str(exc)[:200]


@contextmanager
def connect():
    if ENGINE == "postgres":
        global _pg_pool
        if _pg_pool is None:
            _pg_open_pool()
        try:
            cm = _pg_pool.connection()
        except Exception:
            _pg_open_pool()  # stale pool after a DB restart — rebuild once
            cm = _pg_pool.connection()
        with cm as real:
            yield _PgConn(real)
        return
    with _lock:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=8000")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


# schema literal shared by both engines (Postgres swaps AUTOINCREMENT -> IDENTITY)
_SCHEMA_SQLITE = """
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

            CREATE TABLE IF NOT EXISTS worker_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS token_cache (
                ca TEXT PRIMARY KEY,
                chain TEXT,
                payload TEXT,
                updated_at REAL
            );
            """

# (table, column, decl) migrations applied on every boot, both engines
_COLUMN_MIGRATIONS = [
    ("monitors", "qty", "TEXT"),
    ("monitors", "cost", "TEXT"),
    ("users", "premium_until", "REAL DEFAULT 0"),
    ("users", "cashback", "TEXT DEFAULT '{}'"),
    ("users", "cashback_lifetime", "TEXT DEFAULT '{}'"),
    ("users", "fee_credit", "TEXT DEFAULT '{}'"),
    ("wallets", "sort_order", "INTEGER DEFAULT 0"),
    ("copytrade", "buy_pct", "TEXT"),
    ("orders", "last_fire", "REAL DEFAULT 0"),
]


def init_db() -> None:
    if ENGINE == "postgres":
        with connect() as con:
            con.executescript(_SCHEMA_SQLITE.replace(
                "INTEGER PRIMARY KEY AUTOINCREMENT",
                "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            ))

            def _has_col(table: str, name: str) -> bool:
                cur = con.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
                    (table, name),
                )
                return int(cur.fetchone()["n"]) > 0

            for table, name, decl in _COLUMN_MIGRATIONS:
                if not _has_col(table, name):
                    con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {decl}')
        return
    with connect() as con:
        con.executescript(_SCHEMA_SQLITE)

        def _col(table: str, name: str, decl: str) -> None:
            existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

        for _t, _n, _d in _COLUMN_MIGRATIONS:
            _col(_t, _n, _d)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass


def is_premium(user: dict | None) -> bool:
    if not user:
        return False
    until = float(user.get("premium_until") or 0)
    return bool(user.get("premium")) and until > time.time()


def cap(user: dict | None, kind: str) -> int:
    p = is_premium(user)
    table = {
        "wallets": (MAX_WALLETS_FREE, MAX_WALLETS_PREMIUM),
        "copy": (MAX_COPY_FREE, MAX_COPY_PREMIUM),
        "snipe": (MAX_SNIPE_FREE, MAX_SNIPE_PREMIUM),
        "orders": (MAX_ORDER_FREE, MAX_ORDER_PREMIUM),
        "monitors": (MAX_MONITOR_FREE, MAX_MONITOR_PREMIUM),
        "trending": (TRENDING_FREE, TRENDING_PREMIUM),
    }
    lo, hi = table.get(kind, (1, 1))
    return hi if p else lo


def cap_alert(user: dict | None, kind: str) -> str:
    labels = {
        "wallets": "wallets per chain",
        "copy": "copytrade wallets",
        "snipe": "concurrent snipes",
        "orders": "limit orders",
        "monitors": "trade monitors",
    }
    free_n = cap({"premium": 0, "premium_until": 0}, kind)
    prem_n = cap({"premium": 1, "premium_until": time.time() + 10}, kind)
    return (
        f"⭐ Free plan allows {free_n} {labels.get(kind, kind)}. "
        f"Premium unlocks {prem_n}. Open /premium to upgrade."
    )


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
                "INSERT INTO chain_prefs (user_id, chain, enabled) VALUES (?, ?, 1) ON CONFLICT (user_id, chain) DO NOTHING",
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
            "SELECT * FROM wallets WHERE user_id=? AND chain=? ORDER BY sort_order, id",
            (user_id, chain),
        ).fetchall()
        return [dict(r) for r in rows]


def swap_wallet_order(user_id: int, chain: str, wallet_id: int, direction: int) -> None:
    wallets = list_wallets(user_id, chain)
    idx = next((i for i, w in enumerate(wallets) if w["id"] == wallet_id), None)
    if idx is None:
        return
    j = idx + direction
    if j < 0 or j >= len(wallets):
        return
    wallets[idx], wallets[j] = wallets[j], wallets[idx]
    with connect() as con:
        for i, w in enumerate(wallets):
            con.execute("UPDATE wallets SET sort_order=? WHERE id=?", (i, w["id"]))


def _json_map(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def add_cashback(user_id: int, chain: str, amount: str) -> None:
    user = get_user(user_id)
    if not user:
        return
    cb = _json_map(user.get("cashback"))
    life = _json_map(user.get("cashback_lifetime"))
    from decimal import Decimal

    add = Decimal(str(amount))
    cb[chain] = str(Decimal(cb.get(chain) or "0") + add)
    life[chain] = str(Decimal(life.get(chain) or "0") + add)
    update_user(user_id, cashback=json.dumps(cb), cashback_lifetime=json.dumps(life))


def claim_cashback(user_id: int) -> dict:
    user = get_user(user_id)
    if not user:
        return {}
    cb = _json_map(user.get("cashback"))
    credit = _json_map(user.get("fee_credit"))
    from decimal import Decimal

    moved = {}
    for chain, val in cb.items():
        amt = Decimal(val or "0")
        if amt <= 0:
            continue
        credit[chain] = str(Decimal(credit.get(chain) or "0") + amt)
        moved[chain] = str(amt)
    update_user(user_id, cashback="{}", fee_credit=json.dumps(credit))
    return moved


def take_fee_credit(user_id: int, chain: str, amount) -> bool:
    """Consume fee credit. True if the protocol fee should be skipped."""
    from decimal import Decimal

    user = get_user(user_id)
    if not user:
        return False
    credit = _json_map(user.get("fee_credit"))
    have = Decimal(credit.get(chain) or "0")
    need = Decimal(str(amount))
    if have < need:
        return False
    credit[chain] = str(have - need)
    update_user(user_id, fee_credit=json.dumps(credit))
    return True


def cashback_summary(user_id: int) -> tuple[dict, dict]:
    user = get_user(user_id) or {}
    return _json_map(user.get("cashback")), _json_map(user.get("cashback_lifetime"))


def wallet_count(user_id: int, chain: str) -> int:
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM wallets WHERE user_id=? AND chain=?",
            (user_id, chain),
        ).fetchone()
        return int(row["c"])


def max_wallets(user: dict) -> int:
    return cap(user, "wallets")


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


def update_order(item_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [item_id]
    with connect() as con:
        con.execute(f"UPDATE orders SET {keys} WHERE id=?", vals)


def trade_stats(user_id: int) -> dict:
    with connect() as con:
        total = con.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE user_id=?", (user_id,)
        ).fetchone()["c"]
        buys = con.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE user_id=? AND side='buy'",
            (user_id,),
        ).fetchone()["c"]
        sells = con.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE user_id=? AND side='sell'",
            (user_id,),
        ).fetchone()["c"]
        ahead = con.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT user_id, COUNT(*) AS n FROM trades GROUP BY user_id
            ) WHERE n > ?
            """,
            (int(total),),
        ).fetchone()["c"]
    return {
        "total": int(total),
        "buys": int(buys),
        "sells": int(sells),
        "rank": int(ahead) + 1,
    }


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


# ----- worker persistent state (survives restart) -----

def worker_get(key: str, default: str = "") -> str:
    with connect() as con:
        row = con.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def worker_set(key: str, value: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO worker_state(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )


def integrity_ok() -> bool:
    try:
        with connect() as con:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return bool(row and str(row[0]).lower() == "ok")
    except Exception:
        return False


# ----- token cache (so 2yr-old / zero-volume tokens still open instantly) -----

TOKEN_CACHE_TTL_LIVE = 45
TOKEN_CACHE_TTL_DEAD = 6 * 3600


def token_cache_get(ca: str) -> dict | None:
    try:
        with connect() as con:
            row = con.execute("SELECT payload, updated_at FROM token_cache WHERE ca=?", (ca.lower(),)).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"] or "{}")
            age = time.time() - float(row["updated_at"] or 0)
            live = bool(payload.get("price") or payload.get("liq"))
            ttl = TOKEN_CACHE_TTL_LIVE if live else TOKEN_CACHE_TTL_DEAD
            if age > ttl:
                return None
            payload["_cached_age"] = int(age)
            return payload
    except Exception:
        return None


def token_cache_put(ca: str, chain: str, payload: dict) -> None:
    try:
        slim = {k: payload.get(k) for k in (
            "ok", "ca", "chain", "symbol", "name", "price", "mc", "liq",
            "change", "dex", "pair_url", "pair_address", "warning", "h1",
            "vol", "created", "sources", "decimals", "supply", "holders",
        )}
        with connect() as con:
            con.execute(
                "INSERT INTO token_cache(ca, chain, payload, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(ca) DO UPDATE SET chain=excluded.chain, payload=excluded.payload, updated_at=excluded.updated_at",
                (ca.lower(), chain, json.dumps(slim), time.time()),
            )
    except Exception:
        pass
