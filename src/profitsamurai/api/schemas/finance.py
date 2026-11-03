"""Request and response for a financial report import."""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, model_validator

from profitsamurai.integrations.yandex_market.reports import require_calendar_month


class FinanceImportCreate(BaseModel):
    period_from: date
    period_to: date

    @model_validator(mode="after")
    def one_calendar_month(self) -> "FinanceImportCreate":
        require_calendar_month(self.period_from, self.period_to)
        return self


class FinanceReportOut(BaseModel):
    report_type: str
    campaign_id: int | None
    status: str
    version: int
    row_count: int
    error_message: str | None
    yandex_report_id: str | None


class FinanceImportOut(BaseModel):
    shop_id: UUID
    business_id: int
    period_from: date
    period_to: date
    version: int
    reports: list[FinanceReportOut]
