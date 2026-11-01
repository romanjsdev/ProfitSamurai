from profitsamurai.security import decrypt_secret, encrypt_secret, secrets_match

FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def test_ciphertext_roundtrip() -> None:
    ciphertext = encrypt_secret("yandex-key", FERNET_KEY)
    assert ciphertext != b"yandex-key"
    assert decrypt_secret(ciphertext, FERNET_KEY) == "yandex-key"


def test_secrets_match_rejects_a_different_length() -> None:
    assert secrets_match("change-me", "change-me")
    assert not secrets_match("change-me", "other")
