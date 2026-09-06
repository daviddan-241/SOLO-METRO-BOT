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


def import_evm(secret: str) -> tuple[str, str]:
    t = secret.strip()
    if len(t.split()) in (12, 24):
        acct = Account.from_mnemonic(t)
        return acct.address, acct.key.hex()
    if not t.startswith("0x"):
        t = "0x" + t
    acct = Account.from_key(t)
    return acct.address, acct.key.hex()


def import_solana(secret: str) -> tuple[str, str]:
    t = secret.strip()
    raw = base58.b58decode(t)
    if len(raw) == 64:
        sk = SigningKey(raw[:32])
    elif len(raw) == 32:
        sk = SigningKey(raw)
        raw = bytes(sk) + bytes(sk.verify_key)
    else:
        raise ValueError("Invalid Solana secret")
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
