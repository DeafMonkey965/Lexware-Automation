from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from models import AnalysisResult


def cents(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validate_analysis(result: AnalysisResult, voucher_id: str, file_id: str | None = None) -> AnalysisResult:
    result.lexware_voucher_id = voucher_id
    result.file_id = file_id
    if result.status in {"IGNORIEREN_ZBON", "IGNORIEREN_NICHT_BUCHEN"}:
        return result
    warnings = list(result.warnings)
    missing = [label for label, value in (("Lieferant", result.supplier), ("Rechnungsnummer", result.invoice_number), ("Datum", result.date)) if not value]
    warnings.extend(f"{label} nicht eindeutig erkannt" for label in missing)
    if result.total_gross is None and not result.platform:
        warnings.append("Gesamtbetrag nicht eindeutig erkannt")
    if not result.tax_groups and not result.platform:
        warnings.append("Steuergruppen nicht eindeutig erkannt")
    for group in result.tax_groups:
        if None in (group.net, group.tax, group.gross):
            warnings.append(f"Steuergruppe {group.rate:g}% unvollstaendig")
        if None not in (group.net, group.tax, group.gross):
            if abs(cents(group.net + group.tax) - cents(group.gross)) > Decimal("0.02"):
                warnings.append(f"Steuergruppe {group.rate:g}%: Netto + Steuer != Brutto")
            if group.rate >= 0 and abs((group.net * (1 + group.rate / 100)) - group.gross) > 0.03:
                warnings.append(f"Steuersatz {group.rate:g}% mathematisch unplausibel")
            if abs(cents(group.net * group.rate / 100) - cents(group.tax)) > Decimal("0.02"):
                warnings.append(f"Steuerbetrag {group.rate:g}% mathematisch unplausibel")
    if None not in (result.total_net, result.total_tax, result.total_gross):
        if abs(cents(result.total_net + result.total_tax) - cents(result.total_gross)) > Decimal("0.02"):
            warnings.append("Gesamt Netto + Steuer != Brutto")
    for field, total in (("net", result.total_net), ("tax", result.total_tax)):
        if total is not None and result.tax_groups and all(getattr(g, field) is not None for g in result.tax_groups):
            if abs(cents(sum(getattr(g, field) for g in result.tax_groups)) - cents(total)) > Decimal("0.02"):
                warnings.append(f"Summe der Steuergruppen ({field}) != Gesamtbetrag")
    if result.platform:
        platform = result.platform
        core_values = (
            platform.restaurant_7_gross, platform.restaurant_19_gross,
            platform.commission_net, platform.commission_tax,
            platform.fees_net, platform.fees_tax, platform.payout,
        )
        optional_values = (platform.tips, platform.corrections, platform.credits, platform.refunds, platform.other_items)
        local_complete = not result.analysis_method or all(value is not None for value in optional_values)
        platform.expected_payout = None
        platform.difference = None
        if all(value is not None for value in core_values) and local_complete:
            expected = (
                platform.restaurant_7_gross + platform.restaurant_19_gross
                + (platform.tips or 0) + (platform.corrections or 0)
                + (platform.credits or 0) + (platform.refunds or 0)
                + (platform.other_items or 0)
                - platform.commission_net - platform.commission_tax
                - platform.fees_net - platform.fees_tax
            )
            platform.expected_payout = round(expected, 2)
            platform.difference = round(platform.payout - platform.expected_payout, 2)
        else:
            warnings.append("Plattform-Kontrollrechnung unvollstaendig: notwendige Zahlen fehlen")
    if result.total_gross is not None and result.tax_groups and all(group.gross is not None for group in result.tax_groups):
        total = sum((group.gross or 0) for group in result.tax_groups)
        if abs(cents(total) - cents(result.total_gross)) > Decimal("0.02"):
            warnings.append("Summe der Steuergruppen != Gesamtbetrag")
    if result.platform and result.platform.difference is not None and abs(result.platform.difference) > 0.02:
        warnings.append("Plattform-Kontrollrechnung weicht um mehr als 0,02 EUR ab")
    result.warnings = sorted(set(warnings))
    math_errors = [warning for warning in warnings if "!=" in warning or "mathematisch unplausibel" in warning or "weicht um" in warning]
    if math_errors:
        result.status = "PRUEFEN"
    elif missing or result.warnings:
        result.status = "UNVOLLSTAENDIG" if missing or result.total_gross is None else "PRUEFEN"
    else:
        result.status = "GRUEN"
    return result
