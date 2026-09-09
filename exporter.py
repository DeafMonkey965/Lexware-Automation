from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from models import AnalysisResult


HEADERS = ["Status", "Lieferant", "Datum", "Rechnungsnummer", "Gesamt", "7%-Brutto", "19%-Brutto", "Kategorie", "Plattform", "Provision", "Gebuehren", "Auszahlung", "Differenz", "Warnungen", "Lexware-ID", "Dokumenttyp", "Analyse-Methode", "Confidence", "Trinkgeld", "Umsaetze"]


def export_results(results: list[AnalysisResult], output: Path = Path("output")) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "vorschlaege.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=HEADERS)
        writer.writeheader()
        for item in results:
            groups = {int(group.rate): group.gross for group in item.tax_groups}
            platform = item.platform
            status = "IGNORIERT - Z-BON" if item.status == "IGNORIEREN_ZBON" else item.status
            writer.writerow({"Status": status, "Lieferant": item.supplier, "Datum": item.date, "Rechnungsnummer": item.invoice_number, "Gesamt": item.total_gross, "7%-Brutto": groups.get(7), "19%-Brutto": groups.get(19), "Kategorie": item.category, "Plattform": platform.platform if platform else None, "Provision": platform.commission_net if platform else None, "Gebuehren": platform.fees_net if platform else None, "Auszahlung": platform.payout if platform else None, "Differenz": platform.difference if platform else None, "Warnungen": "; ".join(item.warnings), "Lexware-ID": item.lexware_voucher_id, "Dokumenttyp": item.document_type, "Analyse-Methode": item.analysis_method, "Confidence": item.confidence, "Trinkgeld": platform.tips if platform else None, "Umsaetze": platform_sales(item)})
    cards = []
    for item in results:
        payload = html.escape(json.dumps(item.as_json(), ensure_ascii=False, indent=2))
        label = "IGNORIERT - Z-BON" if item.status == "IGNORIEREN_ZBON" else item.status
        groups = {int(group.rate): group.gross for group in item.tax_groups}
        fields = {"Voucher-ID": item.lexware_voucher_id, "Dokumenttyp": item.document_type,
                  "Lieferant": item.supplier, "Datum": item.date, "Rechnungsnummer": item.invoice_number,
                  "Brutto": item.total_gross, "7 % Brutto": groups.get(7), "19 % Brutto": groups.get(19),
                  "Kategorie-Vorschlag": item.category, "Status": label, "Analyse-Methode": item.analysis_method,
                  "Confidence": item.confidence, "Warnungen": "; ".join(item.warnings)}
        if item.platform:
            p = item.platform
            fields.update({"Plattform": p.platform, "Umsaetze": platform_sales(item),
                           "Provision Netto": p.commission_net, "Provision USt": p.commission_tax,
                           "Gebuehren Netto": p.fees_net, "Gebuehren USt": p.fees_tax,
                           "Trinkgeld": p.tips, "Auszahlung": p.payout,
                           "Erwartete Auszahlung": p.expected_payout, "Kontrolldifferenz": p.difference})
        visible = "".join(f"<dt>{html.escape(key)}</dt><dd>{html.escape(str(value) if value is not None else 'null')}</dd>" for key, value in fields.items())
        cards.append(f'<article class="{item.status.lower()}"><h2>{html.escape(label)} - {html.escape(item.supplier or "Unbekannt")}</h2><dl>{visible}</dl><details><summary>Alle Daten</summary><pre>{payload}</pre></details></article>')
    (output / "pruefung.html").write_text("<!doctype html><meta charset='utf-8'><title>Belegpruefung</title><style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;background:#f4f1ea;color:#252525}article{background:white;border-left:8px solid;padding:1rem;margin:1rem 0}.gruen{border-color:#27834b}.pruefen{border-color:#c99916}.unvollstaendig,.sonderfall{border-color:#b33a3a}pre{white-space:pre-wrap}</style><h1>Belegpruefung</h1>" + "".join(cards), encoding="utf-8")


def platform_sales(item: AnalysisResult) -> float | None:
    p = item.platform
    if p and p.restaurant_7_gross is not None and p.restaurant_19_gross is not None:
        return round(p.restaurant_7_gross + p.restaurant_19_gross, 2)
    return None
