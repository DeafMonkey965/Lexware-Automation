from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Status = Literal["GRUEN", "PRUEFEN", "UNVOLLSTAENDIG", "SONDERFALL", "IGNORIEREN_ZBON", "IGNORIEREN_NICHT_BUCHEN", "IGNORIEREN_KONTOAUSZUG", "IGNORIEREN_KARTENBELEG"]


class TaxGroup(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    rate: float
    net: float | None = None
    tax: float | None = None
    gross: float | None = None


class PlatformAnalysis(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    platform: str | None = None
    period: str | None = None
    settlement_period_from: str | None = None
    settlement_period_to: str | None = None
    restaurant_7_gross: float | None = None
    restaurant_19_gross: float | None = None
    tips: float | None = None
    commission_net: float | None = None
    commission_tax: float | None = None
    fees_net: float | None = None
    fees_tax: float | None = None
    credits: float | None = None
    corrections: float | None = None
    refunds: float | None = None
    other_items: float | None = None
    payout: float | None = None
    expected_payout: float | None = None
    difference: float | None = None


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    supplier: str | None = None
    invoice_number: str | None = None
    date: str | None = None
    document_type: str | None = None
    total_gross: float | None = None
    total_net: float | None = None
    total_tax: float | None = None
    due_date: str | None = None
    currency: str | None = None
    supplier_vat_id: str | None = None
    supplier_iban: str | None = None
    special_items: list[str] = Field(default_factory=list)
    analysis_method: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    tax_groups: list[TaxGroup] = Field(default_factory=list)
    category: str | None = None
    hints: list[str] = Field(default_factory=list)
    platform: PlatformAnalysis | None = None
    status: Status = "UNVOLLSTAENDIG"
    warnings: list[str] = Field(default_factory=list)
    lexware_voucher_id: str | None = None
    file_id: str | None = None
    analyzed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
