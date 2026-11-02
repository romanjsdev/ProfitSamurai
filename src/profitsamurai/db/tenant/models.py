"""Tables inside one client's physical database."""

from sqlalchemy import BigInteger, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TenantBase(DeclarativeBase):
    pass


class TenantCampaign(TenantBase):
    """A Yandex campaign that belongs to a shop inside this client's database."""

    __tablename__ = "campaigns"
    __table_args__ = {"schema": "ref"}

    campaign_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(BigInteger, index=True)
    placement_type: Mapped[str | None] = mapped_column(Text)
