from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.security import decrypt, encrypt


class EncryptedString(TypeDecorator):
    """Texto guardado criptografado (Fernet) no banco e em claro no Python."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt(value)
