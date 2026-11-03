"""Service database registry.

Tables are created by the explicit operations in revision 001_registry.
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
    """Metadata for clients, client_databases, and shops."""


class Client(ServiceBase):
    """A seller company. This is not a Yandex cabinet and not a person."""

    __tablename__ = "clients"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class ClientDatabase(ServiceBase):
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


class Shop(ServiceBase):
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
