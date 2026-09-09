from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from models import AnalysisResult
from validator import validate_analysis


def build_fallback_analysis(filename: str) -> AnalysisResult:
    return AnalysisResult(document_type="Unbekannt", hints=[f"Dokument {filename} konnte ohne KI nicht analysiert werden"], warnings=["OPENAI_API_KEY fehlt oder Analyse nicht verfuegbar"])


class OpenAIAnalyzer:
    def __init__(self, api_key: str | None, model: str = "gpt-4.1-mini"):
        self.api_key = api_key
        self.model = model
        self.disabled_reason: str | None = None
        self.calls = 0

    def analyze(self, file_path: Path, voucher_id: str, file_id: str | None = None) -> AnalysisResult:
        return self.analyze_files([file_path], voucher_id, [file_id] if file_id else [])

    def analyze_files(self, file_paths: list[Path], voucher_id: str, file_ids: list[str] | None = None) -> AnalysisResult:
        file_ids = file_ids or []
        if not file_paths:
            return validate_analysis(AnalysisResult(warnings=["Keine Belegdatei vorhanden"]), voucher_id)
        if not self.api_key:
            return validate_analysis(build_fallback_analysis(", ".join(path.name for path in file_paths)), voucher_id, ",".join(file_ids) or None)
        if self.disabled_reason:
            return validate_analysis(AnalysisResult(warnings=[self.disabled_reason]), voucher_id, ",".join(file_ids) or None)
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key, max_retries=0, timeout=60)
            content = [{"type": "input_text", "text": self._instructions()}]
            supported = {"application/pdf", "image/jpeg", "image/png", "image/tiff", "application/xml"}
            for file_path in file_paths:
                mime = mimetypes.guess_type(file_path.name)[0]
                if mime not in supported:
                    raise ValueError(f"Datei nicht unterstuetzt: {file_path.name} ({mime or 'unbekannt'})")
                encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
                if mime.startswith("image/"):
                    content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
                else:
                    content.append({"type": "input_file", "filename": file_path.name, "file_data": f"data:{mime};base64,{encoded}"})
            self.calls += 1
            response = client.responses.create(model=self.model, input=[{"role": "user", "content": content}], text={"format": {"type": "json_object"}})
            parsed = json.loads(response.output_text)
            return validate_analysis(AnalysisResult.model_validate(parsed), voucher_id, ",".join(file_ids) or None)
        except Exception as exc:
            if any(marker in str(exc).lower() for marker in ("invalid_api_key", "credit_balance_exhausted", "insufficient_quota")) or getattr(exc, "status_code", None) == 401:
                self.disabled_reason = "OpenAI Analyse fehlgeschlagen: API-Key oder Guthaben ungueltig; weitere Aufrufe fuer diesen Lauf deaktiviert"
            result = build_fallback_analysis(", ".join(path.name for path in file_paths))
            result.warnings = [f"OpenAI Analyse fehlgeschlagen: {exc}"]
            return validate_analysis(result, voucher_id, ",".join(file_ids) or None)

    @staticmethod
    def _instructions() -> str:
        return """Analysiere diesen Gastronomie-Beleg. Antworte ausschliesslich als JSON passend zu diesem Schema: supplier, invoice_number, date (YYYY-MM-DD), document_type, total_gross, tax_groups (Liste mit rate, net, tax, gross), category, hints (Liste), platform (Objekt mit platform, period, restaurant_7_gross, restaurant_19_gross, tips, commission_net, commission_tax, fees_net, fees_tax, credits, corrections, refunds, other_items, payout), warnings (Liste). document_type muss exakt eine dieser Formen sein: eingangsrechnung, gutschrift, plattformabrechnung, zbon, kontoauszug, kartenzahlungsbeleg, sonstiges. Verwende null, wenn etwas nicht lesbar oder nicht vorhanden ist. Erfinde niemals Zahlen. Bei Lieferando, Wolt oder Uber Eats berechne expected_payout = Umsaetze + corrections + credits - commission_net - commission_tax - fees_net - fees_tax + refunds + other_items und difference = payout - expected_payout."""


# Keep the public analyzer interface; normal callers now use the local pipeline.
from local_analyzer import DocumentAnalyzer  # noqa: E402
