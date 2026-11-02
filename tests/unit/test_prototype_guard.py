import pytest

from profitsamurai.db.prototype_guard import (
    PrototypeGuardError,
    prototype_data_is_present,
    require_prototype_replacement,
)
from profitsamurai.security import encrypt_secret

FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def test_empty_prototype_tables_do_not_require_the_live_shop() -> None:
    assert prototype_data_is_present(set(), set()) is False


def test_known_business_requires_the_new_connection() -> None:
    ciphertext = encrypt_secret("shop-key", FERNET_KEY)
    require_prototype_replacement(
        [("connected", ciphertext, "client_abc")],
        {"client_abc": {129958161: "FBY", 148633332: "FBS"}},
        FERNET_KEY,
    )
    assert prototype_data_is_present({173963462}, set()) is True


def test_wrong_placement_blocks_the_drop() -> None:
    ciphertext = encrypt_secret("shop-key", FERNET_KEY)
    with pytest.raises(PrototypeGuardError, match="148633332"):
        require_prototype_replacement(
            [("connected", ciphertext, "client_abc")],
            {"client_abc": {129958161: "FBY", 148633332: "DBS"}},
            FERNET_KEY,
        )


def test_ciphertext_must_decrypt_without_exposing_it() -> None:
    with pytest.raises(PrototypeGuardError, match="could not be decrypted"):
        require_prototype_replacement(
            [("connected", b"not-a-token", "client_abc")],
            {"client_abc": {129958161: "FBY", 148633332: "FBS"}},
            FERNET_KEY,
        )
