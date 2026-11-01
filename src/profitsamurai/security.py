"""Api-Key at rest, and a constant-time check for the bootstrap token."""

import hmac

from cryptography.fernet import Fernet


def encrypt_secret(secret: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(secret.encode())


def decrypt_secret(ciphertext: bytes, fernet_key: str) -> str:
    return Fernet(fernet_key.encode()).decrypt(ciphertext).decode()


def secrets_match(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())
