"""Service database. One Postgres database for the first slice."""

from sqlalchemy import BigInteger, ForeignKey, LargeBinary
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ServiceBase(DeclarativeBase):
    pass


class Cabinet(ServiceBase):
    __tablename__ = "cabinets"

    business_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    api_key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)


class Campaign(ServiceBase):
    __tablename__ = "campaigns"

    campaign_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.business_id", ondelete="CASCADE"),
        index=True,
    )
