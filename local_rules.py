from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from models import AnalysisResult, PlatformAnalysis, TaxGroup
from preflight import detect_zbon_text


MONEY = r"(?<![\d.,])[-+]?(?:\d{1,3}(?:[.\u00a0 ]\d{3})+|\d+)[,.]\d{2}(?!\d)"
DATE = r"(?:\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})"
INVOICE_MARKER = (r"\b(?:Rechnungsnummer|Rechnung[ \t]+Nr\.?|Rechnungs[- ]?Nr\.?|"
                  r"Re\.[ \t]*-[ \t]*Nr\.?|Belegnummer|Invoice[ \t]*(?:No\.?|Number)|Gutschriftsnummer)(?!\w)")
INVOICE_STOPWORDS = {'datum', 'rechnung', 'rechnungsdatum', 'kundennummer', 'kunde',
                     'seite', 'beleg', 'mwst', 'netto', 'brutto'}


def invoice_number_candidates(text: str) -> list[dict]:
    candidates = []
    for match in re.finditer(INVOICE_MARKER, text, re.I):
        # Same line or the immediately following line; never scan past another label.
        tail = text[match.end():]
        value_match = re.match(r'[ \t]*[:=]?[ \t]*(?:\r?\n[ \t]*)?([^\s:;=|]+)', tail)
        value = value_match.group(1).rstrip('.,;') if value_match else ''
        reason = None
        if not value:
            reason = 'Kein Wert direkt am Marker'
        elif value.casefold() in INVOICE_STOPWORDS:
            reason = 'Feldbezeichnung statt Rechnungsnummer'
        elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_.-]{0,63}', value) or not re.search(r'\d', value):
            reason = 'Kein plausibler Nummernwert mit Ziffer'
        elif re.fullmatch(DATE, value) or re.fullmatch(MONEY, value):
            reason = 'Datum oder Geldbetrag statt Rechnungsnummer'
        candidates.append(dict(marker=match.group(), value=value or None, accepted=reason is None,
                               reason=reason or 'Plausibler Wert direkt am Marker',
                               line=text.count('\n', 0, match.start()) + 1))
    return candidates


def debug_candidates(text: str, registry: Path = Path('supplier_rules.json')) -> dict:
    diagnostic_date = r'(?<!\d)(?:\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2})(?!\d)'
    def matches(pattern, exclude_dates=False):
        dates = [m.span() for m in re.finditer(diagnostic_date, text)] if exclude_dates else []
        return [dict(value=m.group(), line=text.count('\n', 0, m.start()) + 1,
                     context=text[text.rfind('\n', 0, m.start()) + 1:
                                  text.find('\n', m.end()) if '\n' in text[m.end():] else len(text)])
                for m in re.finditer(pattern, text, re.I)
                if not any(start < m.end() and m.start() < end for start, end in dates)]
    suppliers, warnings = [], []
    supplier_from_text(text, warnings, registry, candidates=suppliers)
    return {'Rechnungsnummer': invoice_number_candidates(text), 'Datum': matches(diagnostic_date),
            'Geldbetraege': matches(MONEY, exclude_dates=True),
            'Steuersaetze': matches(r'(?<![\d.,])(?:7|19)\s*%'),
            'Prozentangaben_ohne_Steuerbestaetigung': matches(r'(?<![\d.,])\d{1,2}(?:[.,]\d+)?\s*%'),
            'Lieferanten': suppliers, 'Lieferantenwarnungen': warnings}


def amount(value: str) -> float:
    value = value.replace("\u00a0", "").replace(" ", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    return float(Decimal(value))


def unique(values: list, label: str, warnings: list[str]):
    choices = list(dict.fromkeys(values))
    if len(choices) > 1:
        warnings.append(f"{label} mehrdeutig erkannt")
    return choices[0] if len(choices) == 1 else None


def labeled_money(text: str, labels: str, warnings: list[str], name: str) -> float | None:
    # Only explicitly labelled values; never infer totals from the largest number.
    pattern = rf"^\s*(?:{labels})\s*(?:EUR|€)?\s*[:=]?\s*({MONEY})\s*(?:EUR|€)?\s*$"
    return unique([amount(m.group(1)) for m in re.finditer(pattern, text, re.I | re.M)], name, warnings)


def labeled_date(text: str, labels: str, warnings: list[str], name: str) -> str | None:
    values = []
    for match in re.finditer(rf"(?:{labels})\s*:?\s*({DATE})", text, re.I):
        raw = match.group(1)
        try:
            values.append(datetime.strptime(raw, "%d.%m.%Y" if "." in raw else "%Y-%m-%d").date().isoformat())
        except ValueError:
            warnings.append(f"{name} ungueltig")
    return unique(values, name, warnings)


def classify(text: str) -> str:
    if detect_zbon_text(text)[0]:
        return "zbon"
    if re.search(r"kontoauszug|kontobewegung|bank statement", text, re.I):
        return "kontoauszug"
    if re.search(r"kartenzahlung|kartenbeleg|ec[- ]?zahlung|credit card", text, re.I):
        return "kartenzahlungsbeleg"
    if re.search(r"\b(lieferando|wolt|uber\s*eats)\b", text, re.I):
        return "plattformabrechnung"
    if re.search(r"^\s*(?:Gutschrift|Gutschriftsnummer)\b", text, re.I | re.M):
        return "gutschrift"
    if re.search(r"\b(rechnung|rechnungsnummer|invoice)\b", text, re.I):
        return "eingangsrechnung"
    return "sonstiges"


def supplier_from_text(text: str, warnings: list[str], registry: Path, candidates: list | None = None) -> str | None:
    values = re.findall(r"^\s*(?:Lieferant|Rechnungssteller|Verkaeufer|Verkäufer|Supplier)\s*:\s*([^\n]+)", text, re.I | re.M)
    if candidates is not None:
        candidates.extend(dict(value=value.strip(), source='Explizite Lieferantenbezeichnung') for value in values)
    # An unlabelled name/address may be the customer. Only explicit issuer labels
    # or a locally maintained identity rule are accepted.
    if registry.is_file():
        try:
            entries = json.loads(registry.read_text(encoding="utf-8"))
            for entry in entries:
                markers = entry.get("markers", [])
                if markers and all(marker.casefold() in text.casefold() for marker in markers):
                    values.append(entry["name"])
                    if candidates is not None:
                        candidates.append(dict(value=entry['name'], source='Lokale Lieferantenregel', markers=markers))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            warnings.append("Lokale Lieferantenregeln ungueltig")
    return unique([value.strip() for value in values], "Lieferant", warnings)


def tax_groups(text: str, warnings: list[str]) -> list[TaxGroup]:
    groups = []
    # Tables are only interpreted if a header states the column order.
    order = []
    values_by_rate: dict[int, dict[str, list[float]]] = {}
    for line in text.splitlines():
        if re.search(r"(?:Umsatz|Umsätze|Umsaetze|Restaurant sales)\s*(?:7|19)\s*%", line, re.I):
            continue
        labels = re.findall(r"\b(Netto|Steuer|MwSt|USt|Brutto)\b", line, re.I)
        if len(labels) >= 3 and not re.search(MONEY, line):
            order = ["net" if x.lower() == "netto" else "gross" if x.lower() == "brutto" else "tax" for x in labels]
        rate_match = re.search(r"(?<!\d)(7|19)\s*%", line)
        if not rate_match:
            continue
        rate = int(rate_match.group(1))
        values = values_by_rate.setdefault(rate, {"net": [], "tax": [], "gross": []})
        labelled = False
        for label, field in (("Netto", "net"), ("(?:Steuer|MwSt|USt)", "tax"), ("Brutto", "gross")):
            found = re.search(rf"\b{label}\s*[:=]?\s*({MONEY})", line, re.I)
            if found:
                values[field].append(amount(found.group(1)))
                labelled = True
        if not labelled:
            numbers = re.findall(MONEY, line)
            if len(numbers) == 3 and len(order) == 3 and len(set(order)) == 3:
                for field, number in zip(order, numbers):
                    values[field].append(amount(number))
    for rate, values in sorted(values_by_rate.items()):
        parsed = {field: unique(numbers, f"Steuergruppe {rate}% {field}", warnings) for field, numbers in values.items()}
        groups.append(TaxGroup(rate=rate, **parsed))
    return groups


PLATFORM_FIELDS = {
    "restaurant_7_gross": r"(?:Restaurant[- ]?)?(?:Umsaetze|Umsätze|Umsatz|Restaurant sales)\s*7\s*%\s*(?:Brutto)?",
    "restaurant_19_gross": r"(?:Restaurant[- ]?)?(?:Umsaetze|Umsätze|Umsatz|Restaurant sales)\s*19\s*%\s*(?:Brutto)?",
    "tips": r"Trinkgeld|Tips",
    "commission_net": r"(?:Provision|Commission)\s*(?:Netto|Net)",
    "commission_tax": r"(?:Provision|Commission)\s*(?:MwSt|USt|Steuer|VAT)",
    "fees_net": r"(?:Sonstige\s*)?(?:Gebuehren|Gebühren|Fees)\s*(?:Netto|Net)",
    "fees_tax": r"(?:Sonstige\s*)?(?:Gebuehren|Gebühren|Fees)\s*(?:MwSt|USt|Steuer|VAT)",
    "credits": r"Gutschriften|Credits", "corrections": r"Korrekturen|Corrections",
    "refunds": r"Erstattungen|Refunds", "other_items": r"Sonstige Positionen|Other items",
    "payout": r"Auszahlung|Auszahlungsbetrag|Payout",
}


def extract_rules(text: str, registry: Path = Path("supplier_rules.json")) -> AnalysisResult:
    result = AnalysisResult(document_type=classify(text), analysis_method="rules")
    if result.document_type == "zbon":
        result.status = "IGNORIEREN_ZBON"
        result.warnings = [detect_zbon_text(text)[1] or "Z-Bon erkannt"]
        result.confidence = 1.0
        return result
    if result.document_type in {"kontoauszug", "kartenzahlungsbeleg"}:
        result.status = "IGNORIEREN_NICHT_BUCHEN"
        result.warnings = ["Nicht buchbarer Beleg erkannt"]
        result.confidence = 1.0
        return result
    warnings = result.warnings
    result.supplier = supplier_from_text(text, warnings, registry)
    result.invoice_number = unique([c['value'] for c in invoice_number_candidates(text) if c['accepted']],
                                   "Rechnungsnummer", warnings)
    result.date = labeled_date(text, r"Rechnungsdatum|Belegdatum|Invoice date", warnings, "Rechnungsdatum")
    result.due_date = labeled_date(text, r"Fällig(?:keit| am)?|Faellig(?:keit| am)?|Zahlbar bis|Due date", warnings, "Faelligkeit")
    result.total_gross = labeled_money(text, r"Gesamt(?:betrag|\s*Brutto)?|Bruttobetrag|Rechnungsbetrag|Endbetrag|Summe\s*Brutto", warnings, "Gesamt Brutto")
    result.total_net = labeled_money(text, r"Gesamt\s*Netto|Nettobetrag|Summe\s*Netto|Netto", warnings, "Gesamt Netto")
    result.total_tax = labeled_money(text, r"Gesamt\s*(?:Umsatzsteuer|MwSt|USt|Steuer)|Steuerbetrag|Umsatzsteuer|MwSt|USt", warnings, "Gesamt Umsatzsteuer")
    currencies = re.findall(r"\b(?:EUR|USD|GBP|CHF)\b|€", text)
    result.currency = unique(["EUR" if c == "€" else c for c in currencies], "Waehrung", warnings)
    result.tax_groups = tax_groups(text, warnings)
    if result.document_type != "plattformabrechnung":
        for label, value in (("Gesamt Netto", result.total_net), ("Gesamt Umsatzsteuer", result.total_tax), ("Waehrung", result.currency)):
            if value is None:
                warnings.append(f"{label} nicht eindeutig erkannt")
    if result.supplier:
        result.supplier_vat_id = unique(re.findall(r"(?:USt[- .]?Id(?:Nr)?\.?|VAT ID)\s*:?\s*([A-Z]{2}[A-Z0-9]{8,12})", text, re.I), "USt-ID", warnings)
        result.supplier_iban = unique(re.findall(r"\bIBAN\s*:?\s*([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30})", text), "IBAN", warnings)
    result.special_items = [line.strip() for line in text.splitlines() if re.search(
        r"\b(?:Pfand|Leergut|Palettenmiete|Miete|Gutschrift|Retour\w*|Rückgabe|Rueckgabe|Rabatt)\b", line, re.I)]
    if result.special_items:
        result.hints.append("Sonderpositionen vorhanden; steuerliche Behandlung manuell pruefen")
        result.warnings.append("Sonderpositionen: steuerliche Behandlung manuell pruefen")
    if result.document_type == "plattformabrechnung":
        names = re.findall(r"\b(lieferando|wolt|uber\s*eats)\b", text, re.I)
        name = unique([re.sub(r"\s+", " ", n.lower()) for n in names], "Plattform", warnings)
        platform = PlatformAnalysis(platform=name)
        for field, labels in PLATFORM_FIELDS.items():
            setattr(platform, field, labeled_money(text, labels, warnings, field))
        platform.settlement_period_from = labeled_date(text, r"Abrechnungszeitraum\s*(?:von)?|Period from", warnings, "Zeitraum von")
        platform.settlement_period_to = labeled_date(text, rf"Abrechnungszeitraum\s*(?:von)?\s*{DATE}\s*(?:bis|–|-)|Period to", warnings, "Zeitraum bis")
        if platform.settlement_period_from and platform.settlement_period_to:
            platform.period = f"{platform.settlement_period_from} / {platform.settlement_period_to}"
        result.platform = platform
    return result
