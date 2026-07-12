import pytest
from cryptography.fernet import InvalidToken

from provgate.store.crypto import SecretBox, generate_key


def test_roundtrip() -> None:
    box = SecretBox(generate_key())
    ct = box.encrypt("hunter2")
    assert ct != b"hunter2"
    assert box.decrypt(ct) == "hunter2"


def test_wrong_key_cannot_decrypt() -> None:
    ct = SecretBox(generate_key()).encrypt("secret")

    with pytest.raises(InvalidToken):
        SecretBox(generate_key()).decrypt(ct)
