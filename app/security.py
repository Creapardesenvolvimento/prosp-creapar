import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def derive_key(purpose: str) -> bytes:
    """Deriva uma chave de 32 bytes da SECRET_KEY para um uso específico."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode())
    return hkdf.derive(get_settings().secret_key.encode())


@lru_cache
def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key("fernet")))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def session_secret() -> str:
    return derive_key("session").hex()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
