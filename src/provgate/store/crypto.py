"""At-rest encryption for stored credentials (Fernet)."""

from __future__ import annotations

from cryptography.fernet import Fernet


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    """Encrypts/decrypts short credential strings with a Fernet key."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode("utf-8")
