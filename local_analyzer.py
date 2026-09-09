from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from local_config import LocalConfig
from local_extractor import LocalDocumentExtractor
from local_rules import extract_rules, MONEY, amount
from models import AnalysisResult
from validator import validate_analysis


def finalize(result: AnalysisResult, voucher_id: str, file_ids: list[str]) -> AnalysisResult:
    result = validate_analysis(result, voucher_id, ",".join(file_ids) or None)
    if result.status.startswith("IGNORIEREN"):
        result.confidence = 1.0
    else:
        # Completeness/plausibility indicator, not a calibrated probability.
        essential = (result.supplier, result.invoice_number, result.date,
                     result.total_gross if not result.platform else result.platform.payout)
        score = sum(value is not None for value in essential) / len(essential)
        result.confidence = round(min(0.95 if result.status == "GRUEN" else 0.65, score * 0.95), 2)
    return result


def sufficient(result: AnalysisResult) -> bool:
    checked = result.model_copy(deep=True)
    checked.warnings = [w for w in checked.warnings if w != "KI-Ergaenzungen manuell mit dem Beleg abgleichen"]
    return validate_analysis(checked, checked.lexware_voucher_id, checked.file_id).status == "GRUEN"


class DocumentAnalyzer:
    def __init__(self, api_key: str | None = None, model: str = "gpt-4.1-mini",
                 config: LocalConfig | None = None, local_only: bool = False):
        self.config = config or LocalConfig.from_env()
        if local_only:
            self.config = replace(self.config, use_openai_fallback=False)
        self.api_key = api_key
        self.model = model
        self.extractor = LocalDocumentExtractor(self.config.save_ocr_text)
        self.ollama_calls = 0
        self.openai_calls = 0
        self.local_calls = 0
        self._openai = None

    def analyze(self, file_path: Path, voucher_id: str, file_id: str | None = None) -> AnalysisResult:
        return self.analyze_files([file_path], voucher_id, [file_id] if file_id else [])

    def analyze_files(self, file_paths: list[Path], voucher_id: str, file_ids: list[str] | None = None) -> AnalysisResult:
        file_ids = file_ids or []
        documents = [self.extractor.extract(path, file_ids[i] if i < len(file_ids) else None)
                     for i, path in enumerate(file_paths)]
        text = "\n\n".join(dict.fromkeys(d.text for d in documents))
        result = extract_rules(text)
        result.analysis_method = "ocr_rules" if any(d.used_ocr for d in documents) else "rules"
        result.hints.insert(0, "Quelldateien: " + ", ".join(path.name for path in file_paths))
        result.warnings.extend(w for d in documents for w in d.warnings)
        if not file_paths:
            result.warnings.append("Keine Belegdatei vorhanden")
        result = finalize(result, voucher_id, file_ids)
        self.local_calls += 1
        if result.status.startswith("IGNORIEREN") or result.status == "GRUEN":
            return result
        if self.config.use_ollama and text.strip():
            try:
                from ollama_client import OllamaClient
                client = OllamaClient(self.config.ollama_base_url, self.config.ollama_model)
                self.ollama_calls += 1
                suggestion = client.analyze(text)
                result = self._merge(result, suggestion, "ollama", voucher_id, file_ids, text)
            except Exception:
                result.warnings.append("Lokale KI nicht verfügbar")
                result = finalize(result, voucher_id, file_ids)
        if self.config.use_openai_fallback and not sufficient(result) and file_paths:
            from analyzer import OpenAIAnalyzer
            if self._openai is None:
                self._openai = OpenAIAnalyzer(self.api_key, self.model)
            suggestion = self._openai.analyze_files(file_paths, voucher_id, file_ids)
            self.openai_calls = self._openai.calls
            if any("OpenAI Analyse fehlgeschlagen" in w or "OPENAI_API_KEY fehlt" in w for w in suggestion.warnings):
                result.warnings.extend(suggestion.warnings)
            else:
                result = self._merge(result, suggestion, "openai", voucher_id, file_ids)
        return finalize(result, voucher_id, file_ids)

    @staticmethod
    def _merge(result: AnalysisResult, suggestion: AnalysisResult, method: str, voucher_id: str, file_ids: list[str], text: str | None = None) -> AnalysisResult:
        # Preserve deterministic evidence and never accept model-provided status/confidence.
        for field in ("supplier", "invoice_number", "date", "total_gross", "total_net", "total_tax",
                      "due_date", "currency", "category", "tax_groups", "platform"):
            current, proposed = getattr(result, field), getattr(suggestion, field)
            if text is not None and proposed is not None and not grounded(proposed, text):
                result.warnings.append(f"KI-Wert nicht im Dokument belegt: {field}")
                continue
            if current is None or current == []:
                setattr(result, field, proposed)
            elif proposed is not None and proposed != [] and current != proposed:
                result.warnings.append(f"KI-Vorschlag widerspricht lokalen Regeln: {field}")
        result.analysis_method += "+" + method
        # Recompute validator diagnostics after filling missing fields.
        result.warnings = [w for w in result.warnings if w not in {
            "Lieferant nicht eindeutig erkannt", "Rechnungsnummer nicht eindeutig erkannt",
            "Datum nicht eindeutig erkannt", "Gesamtbetrag nicht eindeutig erkannt",
            "Steuergruppen nicht eindeutig erkannt", "Plattform-Kontrollrechnung unvollstaendig: notwendige Zahlen fehlen",
        }]
        result.warnings.append("KI-Ergaenzungen manuell mit dem Beleg abgleichen")
        return finalize(result, voucher_id, file_ids)


def grounded(value, text: str) -> bool:
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            y, m, d = value.split("-")
            return value in text or f"{d}.{m}.{y}" in text
        return value.casefold() in text.casefold()
    if isinstance(value, (int, float)):
        return value in [amount(raw) for raw in re.findall(MONEY, text)]
    if isinstance(value, list):
        return all(grounded(item, text) for item in value)
    if hasattr(value, "model_dump"):
        return all(grounded(item, text) for key, item in value.model_dump().items()
                   if item is not None and key not in {"rate", "expected_payout", "difference", "period"})
    return True
