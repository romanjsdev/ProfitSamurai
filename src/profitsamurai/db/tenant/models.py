"""Tables inside one client's physical database."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from profitsamurai.jsonutil import JsonObject


class TenantBase(DeclarativeBase):
    pass


class TenantCampaign(TenantBase):
    """A Yandex campaign that belongs to a shop inside this client's database."""

    __tablename__ = "campaigns"
    __table_args__ = {"schema": "ref"}

    campaign_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(BigInteger, index=True)
    placement_type: Mapped[str | None] = mapped_column(Text)


class ReportImport(TenantBase):
    """One attempt to import a single Yandex report into this client database."""

    __tablename__ = "report_imports"
    __table_args__ = (
        CheckConstraint(
            "report_type IN ('goods_realization', 'marketplace_services', 'united_netting')",
            name="ck_report_imports_type",
        ),
        CheckConstraint(
            "status IN ('running', 'done', 'no_data', 'too_large', 'failed')",
            name="ck_report_imports_status",
        ),
        UniqueConstraint(
            "shop_id",
            "period_from",
            "period_to",
            "report_type",
            "campaign_id",
            "version",
            name="uq_report_imports_version",
            postgresql_nulls_not_distinct=True,
        ),
        {"schema": "raw"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    shop_id: Mapped[UUID] = mapped_column(Uuid)
    business_id: Mapped[int] = mapped_column(BigInteger)
    report_type: Mapped[str] = mapped_column(Text)
    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger)
    yandex_report_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)


class ReportFile(TenantBase):
    """The original downloaded report, kept exactly as Yandex sent it."""

    __tablename__ = "report_files"
    __table_args__ = (
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_report_files_content_sha256",
        ),
        {"schema": "raw"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    import_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("raw.report_imports.id", ondelete="CASCADE"),
        unique=True,
    )
    content: Mapped[bytes] = mapped_column(LargeBinary)
    byte_size: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(Text)


class ReportRow(TenantBase):
    """One parsed row. The payload keeps whatever columns the report contained."""

    __tablename__ = "report_rows"
    __table_args__ = {"schema": "raw"}

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    import_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("raw.report_imports.id", ondelete="CASCADE"),
        index=True,
    )
    sheet: Mapped[str] = mapped_column(Text)
    row_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[JsonObject] = mapped_column(JSONB)


class ImportExclusion(TenantBase):
    """One decision to keep a finished import out of analytics until it is lifted."""

    __tablename__ = "import_exclusions"
    __table_args__ = (
        CheckConstraint("btrim(reason) <> ''", name="ck_import_exclusions_reason"),
        CheckConstraint("btrim(basis) <> ''", name="ck_import_exclusions_basis"),
        Index(
            "uq_import_exclusions_active",
            "import_id",
            unique=True,
            postgresql_where=text("lifted_at IS NULL"),
        ),
        {"schema": "analytics"},
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    import_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("raw.report_imports.id", ondelete="CASCADE"),
    )
    reason: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(Text)
    excluded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
