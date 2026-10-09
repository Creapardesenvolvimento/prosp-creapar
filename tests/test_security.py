import pytest
from cryptography.fernet import InvalidToken

from app.security import decrypt, encrypt, hash_password, verify_password


def test_encrypt_roundtrip():
    token = encrypt("api-key-secreta")
    assert "api-key-secreta" not in token
    assert decrypt(token) == "api-key-secreta"


def test_decrypt_rejeita_token_adulterado():
    token = encrypt("abc")
    with pytest.raises(InvalidToken):
        decrypt(token[:-4] + "AAAA")


def test_password_hash():
    stored = hash_password("minha-senha")
    assert "minha-senha" not in stored
    assert verify_password("minha-senha", stored)
    assert not verify_password("outra", stored)
    assert not verify_password("minha-senha", "lixo")
