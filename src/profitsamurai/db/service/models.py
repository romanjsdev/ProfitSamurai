"""Service database registry.

Revision 001 calls ServiceBase.metadata.create_all. That base stays empty so a
fresh upgrade does not create the registry tables twice: revision 002 creates
them explicitly. Registry models use a separate metadata.
"""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ServiceBase(DeclarativeBase):
    """Metadata used by revision 001. It intentionally has no tables."""


class RegistryBase(DeclarativeBase):
    """Tables created by revision 002_client_registry."""


class Client(RegistryBase):
    """A seller company. This is not a Yandex cabinet and not a person."""

    __tablename__ = "clients"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class ClientDatabase(RegistryBase):
    """Name and provisioning status of that company's physical database."""

    __tablename__ = "client_databases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed')",
            name="ck_client_databases_status",
        ),
    )

    client_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("clients.id", ondelete="CASCADE"),
        primary_key=True,
    )
    database_name: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text)


class Shop(RegistryBase):
    """A marketplace connection owned by one client."""

    __tablename__ = "shops"
    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "marketplace",
            "business_id",
            name="uq_shops_client_marketplace_business",
        ),
        CheckConstraint(
            "connection_status IN ('connected', 'error')",
            name="ck_shops_connection_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    client_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("clients.id", ondelete="CASCADE"),
        index=True,
    )
    marketplace: Mapped[str] = mapped_column(Text)
    business_id: Mapped[int] = mapped_column(BigInteger)
    api_key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    connection_status: Mapped[str] = mapped_column(Text)
