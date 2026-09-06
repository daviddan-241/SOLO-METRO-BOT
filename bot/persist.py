"""Persistence helpers: DB path resolution, backup/restore, encryption-key survival.

Render free disks are ephemeral — redeploys wipe SQLite unless a persistent
disk is mounted at /var/data (paid) or DATABASE backups are restored.
This module makes survival best-effort rock solid:

1. resolve_db_path() — prefers /var/data (Render Disk) > /data > ./data,
   and migrates any existing DB copy forward so users never start empty
   when a disk appears/disappears.
2. backup_loop() — every 5 min copies a consistent snapshot to
   <db>.backup.sqlite + data/deluge.backup.sqlite (whichever differs).
3. restore_if_needed() — on boot, if the primary DB is missing/empty but a
   backup exists anywhere, restore it before init_db().
4. ensure_encryption_key() — ENCRYPTION_KEY env wins; else load
   data/.fernet.key; else derive legacy key from BOT_TOKEN (old installs)
   and persist it to file so future restarts are stable even if the token
   is rotated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("solo-metro.persist")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

FERNET_FILE_CANDIDATES = [
    Path("/var/data/.fernet.key"),
    Path("/data/.fernet.key"),
    DATA_DIR / ".fernet.key",
]

DB_CANDIDATE_DIRS = [Path("/var/data"), Path("/data"), DATA_DIR]
DB_FILENAMES = ("deluge.db", "solo_metro.db")


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".deluge_write_test"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_db_path() -> str:
    """Pick best writable path, migrating existing DB data forward."""
    env = (os.getenv("DB_PATH") or "").strip()
    if env:
        p = Path(env)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            return str(p)
        except OSError:
            log.warning("DB_PATH %s not writable, falling back", env)

    # find existing DB with data anywhere
    existing: Path | None = None
    for d in DB_CANDIDATE_DIRS:
        for fn in DB_FILENAMES:
            cand = d / fn
            try:
                if cand.exists() and cand.stat().st_size > 4096:
                    existing = cand
                    break
            except OSError:
                continue
        if existing:
            break

    for d in DB_CANDIDATE_DIRS:
        if not _writable(d):
            continue
        primary = d / DB_FILENAMES[0]
        # migrate data forward if primary empty but data lives elsewhere
        if existing and existing.resolve() != primary.resolve():
            try:
                if (not primary.exists() or primary.stat().st_size < 4096) and existing.stat().st_size > 4096:
                    shutil.copy2(existing, primary)
                    log.info("migrated DB %s -> %s", existing, primary)
            except OSError as exc:
                log.warning("DB migrate %s: %s", existing, exc)
        return str(primary)
    return str(DATA_DIR / DB_FILENAMES[0])


def backup_paths(primary: str) -> list[Path]:
    prim = Path(primary)
    outs = [prim.with_name(prim.stem + ".backup.sqlite")]
    for d in DB_CANDIDATE_DIRS:
        cand = d / "deluge.backup.sqlite"
        if cand.resolve() != prim.resolve() and cand not in outs:
            outs.append(cand)
    # dedupe while keeping order
    seen, uniq = set(), []
    for p in outs:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def _sqlite_backup(src: str, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        s = sqlite3.connect(src, timeout=30)
        try:
            s.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        d = sqlite3.connect(str(dst), timeout=30)
        try:
            s.backup(d)
        finally:
            d.close()
        s.close()
        return dst.exists() and dst.stat().st_size > 0
    except Exception as exc:
        log.debug("backup %s -> %s: %s", src, dst, exc)
        return False


def backup_now(primary: str) -> list[str]:
    try:
        if not os.path.exists(primary) or os.path.getsize(primary) < 4096:
            return []
    except OSError:
        return []
    done = []
    for dst in backup_paths(primary):
        try:
            if str(dst) == primary:
                continue
            if _sqlite_backup(primary, dst):
                done.append(str(dst))
        except Exception as exc:
            log.debug("backup %s: %s", dst, exc)
    if done:
        log.info("DB backup -> %s", ", ".join(done))
    return done


def integrity_check(db_path: str) -> tuple[bool, str]:
    """PRAGMA quick_check — (healthy, detail). Catches corruption early so a
    backup can be restored before users hit errors."""
    try:
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            ok = bool(row) and str(row[0]).lower() == "ok"
            return ok, str(row[0]) if row else "no result"
        finally:
            con.close()
    except Exception as exc:
        return False, f"integrity check failed: {exc}"


def restore_if_needed(primary: str) -> str:
    """If primary is missing/empty, restore newest backup into place."""
    try:
        if os.path.exists(primary) and os.path.getsize(primary) > 4096:
            return primary
    except OSError:
        pass
    candidates: list[tuple[float, Path]] = []
    search: list[Path] = []
    search.extend(backup_paths(primary))
    for d in DB_CANDIDATE_DIRS:
        for fn in (*DB_FILENAMES, "deluge.backup.sqlite"):
            search.append(d / fn)
    for p in search:
        try:
            if str(p) == primary:
                continue
            if p.exists() and p.stat().st_size > 4096:
                candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not candidates:
        return primary
    candidates.sort(reverse=True)
    newest = candidates[0][1]
    try:
        Path(primary).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(newest, primary)
        log.info("restored DB %s -> %s", newest, primary)
    except OSError as exc:
        log.warning("DB restore %s: %s", newest, exc)
    return primary


def ensure_encryption_key() -> bytes:
    """Return raw 32-byte key material source; caller derives Fernet key."""
    import base64
    import hashlib

    env = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if env:
        return env.encode()
    for f in FERNET_FILE_CANDIDATES:
        try:
            if f.exists():
                data = f.read_text().strip()
                if len(data) >= 16:
                    os.environ["ENCRYPTION_KEY"] = data
                    return data.encode()
        except OSError:
            continue
    # legacy: derive from bot token so old installs keep decrypting,
    # then persist to file so a future token rotation doesn't kill wallets.
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    legacy = (token + "|solo-metro-wallets") if token else "solo-metro-wallets|fallback"
    raw = hashlib.sha256(legacy.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(raw).decode()
    for f in FERNET_FILE_CANDIDATES:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(fernet_key)
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
            log.info("persisted Fernet key to %s (set ENCRYPTION_KEY env to override)", f)
            break
        except OSError:
            continue
    os.environ["ENCRYPTION_KEY"] = fernet_key
    return fernet_key.encode()


async def backup_loop(primary: str, interval: int = 300) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(backup_now, primary)
        except Exception as exc:
            log.debug("backup loop: %s", exc)


def checkpoint(primary: str) -> None:
    try:
        con = sqlite3.connect(primary, timeout=10)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def db_status(primary: str) -> dict:
    out: dict = {"path": primary, "size": 0, "backups": []}
    try:
        out["size"] = os.path.getsize(primary) if os.path.exists(primary) else 0
    except OSError:
        out["size"] = 0
    for b in backup_paths(primary):
        try:
            if b.exists():
                out["backups"].append({"path": str(b), "size": b.stat().st_size, "mtime": int(b.stat().st_mtime)})
        except OSError:
            continue
    out["uptime"] = int(time.time() - _BOOT)
    return out


_BOOT = time.time()
