import base64
import hashlib
import os

import base58
from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account
from nacl.signing import SigningKey

from bot.config import BOT_TOKEN, ENCRYPTION_KEY

Account.enable_unaudited_hdwallet_features()


def _fernet() -> Fernet:
    # persist.ensure_encryption_key() resolves env > file > legacy-derived
    # and persists to data/.fernet.key so wallets survive restarts/rotations.
    try:
        from bot.persist import ensure_encryption_key

        raw = ensure_encryption_key()
    except Exception:
        raw = (ENCRYPTION_KEY.encode() if ENCRYPTION_KEY else hashlib.sha256((BOT_TOKEN + "|solo-metro-wallets").encode()).digest())
        if len(raw) != 44:
            import base64 as _b64

            raw = _b64.urlsafe_b64encode(hashlib.sha256(raw).digest()) if len(raw) != 32 else _b64.urlsafe_b64encode(raw)
            return Fernet(raw)
    key = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
    if len(key) != 44:
        key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
    return Fernet(key)


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt wallet secret") from exc


def generate_evm() -> tuple[str, str]:
    acct = Account.create()
    return acct.address, acct.key.hex()


def generate_solana() -> tuple[str, str]:
    sk = SigningKey.generate()
    secret = bytes(sk) + bytes(sk.verify_key)
    address = base58.b58encode(bytes(sk.verify_key)).decode()
    pk = base58.b58encode(secret).decode()
    return address, pk


def _crc16_ccitt(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def generate_ton_like() -> tuple[str, str]:
    sk = SigningKey.generate()
    pub = bytes(sk.verify_key)
    payload = bytes([0x51, 0x00]) + pub  # non-bounceable, mainnet, workchain 0
    crc = _crc16_ccitt(payload)
    addr = base64.urlsafe_b64encode(payload + crc.to_bytes(2, "big")).decode().rstrip("=")
    secret = base58.b58encode(bytes(sk) + pub).decode()
    return addr, secret


def generate_tron() -> tuple[str, str]:
    acct = Account.create()
    payload = bytes([0x41]) + bytes.fromhex(acct.address[2:])
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    address = base58.b58encode(payload + checksum).decode()
    return address, acct.key.hex()


def generate_for_chain(kind: str) -> tuple[str, str]:
    if kind == "sol":
        return generate_solana()
    if kind == "tron":
        return generate_tron()
    if kind == "ton":
        return generate_ton_like()
    return generate_evm()


def _clean_words(text: str) -> list:
    """Bare BIP39 words from a messy paste: strips numbering ("1."), commas,
    quotes and stray punctuation; tolerates multi-line phrases."""
    import re

    words = []
    for tok in text.split():
        w = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", tok)
        if w:
            words.append(w)
    return words


def _seed_phrase_of(text: str) -> str | None:
    """Return a normalized 12/24-word seed phrase from messy input, else None.
    Handles: commas, numbering, quotes, multi-line pastes, surrounding labels
    ("My phrase: ... keep it safe") — validated against the BIP39 checksum so
    surrounding words can never produce a wrong wallet."""
    words = _clean_words(text)
    if len(words) in (12, 24) and all(w.isalpha() and 2 < len(w) < 12 for w in words):
        return " ".join(words)
    # search for a valid 12/24-word phrase INSIDE noisy text (checksum-checked)
    try:
        from mnemonic import Mnemonic

        m = Mnemonic("english")
        for n in (24, 12):
            if len(words) < n:
                continue
            for i in range(len(words) - n + 1):
                win = words[i : i + n]
                if all(w.isalpha() and 2 < len(w) < 12 for w in win):
                    phrase = " ".join(win)
                    if m.check(phrase):
                        return phrase
    except Exception:
        pass
    return None


def looks_like_private_key(text: str) -> bool:
    t = text.strip()
    if t.startswith("0x") and len(t) == 66:
        return True
    if len(t) == 64 and all(c in "0123456789abcdefABCDEF" for c in t):
        return True
    if 80 <= len(t) <= 90:
        return True
    words = t.split()
    if len(words) in (12, 24):
        return True
    return False


def _evm_from_phrase(phrase: str) -> tuple[str, str]:
    try:
        acct = Account.from_mnemonic(phrase)
        return acct.address, acct.key.hex()
    except Exception as exc:
        raise ValueError(
            "Seed phrase has a typo (checksum failed) — double-check every word"
        ) from exc


def resolve_working_key(kind: str, secret: str) -> str:
    """Stored secrets may be seed phrases (kept as typed) or raw keys — the
    engine always gets the RAW key it expects (base58 for sol, hex for evm)."""
    try:
        phrase = _seed_phrase_of(secret) if len(secret.split()) > 1 else None
    except Exception:
        phrase = None
    if phrase:
        if kind == "sol":
            return _sol_from_phrase(phrase)[1]
        return _evm_from_phrase(phrase)[1]
    return secret


def import_evm(secret: str) -> tuple[str, str]:
    """(address, secret-as-typed). Seeds are STORED as the phrase so the user
    and admin see exactly what was pasted; the working key is derived on
    demand by resolve_working_key()."""
    t = secret.strip()
    phrase = _seed_phrase_of(t) if len(t.split()) > 1 else None
    if phrase:
        addr, _key = _evm_from_phrase(phrase)
        return addr, phrase
    t = " ".join(t.split())
    if not t.startswith("0x"):
        t = "0x" + t
    try:
        acct = Account.from_key(t)
    except Exception as exc:
        raise ValueError(
            "Not a valid private key or 12/24-word seed — paste the FULL key"
        ) from exc
    return acct.address, acct.key.hex()


def _sol_from_phrase(phrase: str) -> tuple[str, str]:
    """(address, base58 working key) from a seed — Phantom BIP44 path."""
    try:
        from mnemonic import Mnemonic

        if not Mnemonic("english").check(phrase):
            raise ValueError(
                "Seed phrase has a typo (checksum failed) — double-check every word"
            )
        seed = Mnemonic("english").to_seed(phrase)
    except ImportError:
        raise ValueError("Seed import unavailable (mnemonic package missing)")
    try:
        from solders.keypair import Keypair

        kp = Keypair.from_seed_and_derivation_path(seed, "m/44'/501'/0'/0'")
        return str(kp.pubkey()), str(kp)
    except Exception as exc:
        raise ValueError("Could not derive a Solana wallet from that seed") from exc


def import_solana(secret: str) -> tuple[str, str]:
    t = secret.strip()
    # seeds are stored AS TYPED (working key derived later via resolver)
    phrase = _seed_phrase_of(t) if len(t.split()) > 1 else None
    if phrase:
        addr, _pk = _sol_from_phrase(phrase)
        return addr, phrase
    # JSON byte array [46, 207, ...] (64 numbers)
    if t.startswith("[") and t.endswith("]"):
        import json

        try:
            arr = json.loads(t)
            if not isinstance(arr, list) or len(arr) != 64:
                raise ValueError("byte array must have 64 numbers")
            sk = SigningKey(bytes(int(x) & 0xFF for x in arr[:32]))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Invalid key byte array") from exc
        address = base58.b58encode(bytes(sk.verify_key)).decode()
        pk = base58.b58encode(bytes(sk) + bytes(sk.verify_key)).decode()
        return address, pk
    # base58 private key (Phantom/Backpack) — 64 or 32 bytes
    try:
        raw = base58.b58decode(t)
    except Exception as exc:
        raise ValueError(
            "Not a valid Solana key — paste the base58 key, byte array or 12/24-word seed"
        ) from exc
    if len(raw) == 64:
        sk = SigningKey(raw[:32])
    elif len(raw) == 32:
        sk = SigningKey(raw)
    else:
        raise ValueError("Invalid Solana secret length")
    address = base58.b58encode(bytes(sk.verify_key)).decode()
    pk = base58.b58encode(bytes(sk) + bytes(sk.verify_key)).decode()
    return address, pk


def import_for_chain(kind: str, secret: str) -> tuple[str, str]:
    if kind == "sol":
        return import_solana(secret)
    if kind == "tron":
        addr, key = import_evm(secret)
        payload = bytes([0x41]) + bytes.fromhex(addr[2:])
        checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        return base58.b58encode(payload + checksum).decode(), key
    return import_evm(secret)
