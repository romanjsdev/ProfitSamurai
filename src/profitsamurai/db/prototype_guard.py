"""Refuse to drop the prototype tables while the live shop is not in the new model."""

from cryptography.fernet import Fernet

PROTOTYPE_BUSINESS_ID = 173963462
PROTOTYPE_PLACEMENTS = {129958161: "FBY", 148633332: "FBS"}


class PrototypeGuardError(RuntimeError):
    """The new shop connection does not yet replace the prototype rows."""


def prototype_data_is_present(
    cabinet_business_ids: set[int],
    campaign_ids: set[int],
) -> bool:
    if PROTOTYPE_BUSINESS_ID in cabinet_business_ids:
        return True
    return bool(set(PROTOTYPE_PLACEMENTS) & campaign_ids)


def require_prototype_replacement(
    shops: list[tuple[str, bytes, str]],
    campaigns_by_database: dict[str, dict[int, str | None]],
    fernet_key: str,
) -> None:
    """Confirm the known shop, its ciphertext, and both campaign placements."""
    if not shops:
        raise PrototypeGuardError("prototype business 173963462 is missing from shops")
    for status, ciphertext, database_name in shops:
        if status != "connected":
            raise PrototypeGuardError("prototype shop connection_status is not connected")
        if len(ciphertext) == 0:
            raise PrototypeGuardError("prototype shop ciphertext is empty")
        _require_decryptable(ciphertext, fernet_key)
        found = campaigns_by_database.get(database_name)
        if found is None:
            raise PrototypeGuardError("prototype client database could not be read")
        for campaign_id, placement in PROTOTYPE_PLACEMENTS.items():
            if found.get(campaign_id) != placement:
                raise PrototypeGuardError(
                    f"campaign {campaign_id} is missing or its placement_type differs"
                )


def _require_decryptable(ciphertext: bytes, fernet_key: str) -> None:
    if fernet_key == "":
        raise PrototypeGuardError("FERNET_KEY is required before dropping prototype tables")
    try:
        plain = Fernet(fernet_key.encode()).decrypt(ciphertext)
    except Exception as exc:
        raise PrototypeGuardError("prototype shop ciphertext could not be decrypted") from exc
    if plain == b"":
        raise PrototypeGuardError("prototype shop ciphertext decrypted to an empty value")
