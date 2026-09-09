from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from models import AnalysisResult


class Database:
    def __init__(self, path: Path = Path("buchhaltung.db")):
        self.connection = sqlite3.connect(path)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS analyses (
            lexware_voucher_id TEXT PRIMARY KEY, file_id TEXT, lieferant TEXT, rechnungsnummer TEXT,
            datum TEXT, belegtyp TEXT, gesamtbetrag REAL, analyse_json TEXT NOT NULL, status TEXT,
            warnungen TEXT, analysiert_am TEXT NOT NULL)""")
        self.connection.commit()

    def has(self, voucher_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM analyses WHERE lexware_voucher_id = ?", (voucher_id,)).fetchone() is not None

    def voucher_ids(self) -> set[str]:
        rows = self.connection.execute("SELECT lexware_voucher_id FROM analyses").fetchall()
        return {row[0] for row in rows}

    def get(self, voucher_id: str) -> AnalysisResult | None:
        row = self.connection.execute("SELECT analyse_json FROM analyses WHERE lexware_voucher_id = ?", (voucher_id,)).fetchone()
        return AnalysisResult.model_validate_json(row[0]) if row else None

    def save(self, result: AnalysisResult) -> None:
        self.connection.execute("""INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            result.lexware_voucher_id, result.file_id, result.supplier, result.invoice_number, result.date,
            result.document_type, result.total_gross, json.dumps(result.as_json(), ensure_ascii=False), result.status,
            json.dumps(result.warnings, ensure_ascii=False), result.analyzed_at))
        self.connection.commit()

    def all(self) -> list[AnalysisResult]:
        rows = self.connection.execute("SELECT analyse_json FROM analyses ORDER BY analysiert_am DESC").fetchall()
        return [AnalysisResult.model_validate(json.loads(row[0])) for row in rows]

    def openai_failures(self) -> list[AnalysisResult]:
        return [result for result in self.all()
                if result.status == "UNVOLLSTAENDIG"
                and (result.document_type or "").lower() != "zbon"
                and has_openai_error(result)]

    def save_ignored(self, voucher_id: str, file_ids: list[str], document_type: str, status: str, warning: str) -> None:
        result = AnalysisResult(
            lexware_voucher_id=voucher_id,
            file_id=",".join(file_ids) or None,
            document_type=document_type,
            status=status,
            warnings=[warning],
            analysis_method="rules",
            confidence=1.0,
        )
        self.save(result)


def has_openai_error(result: AnalysisResult) -> bool:
    warnings = "\n".join(result.warnings).lower().replace("\\_", "_")
    return any(marker in warnings for marker in (
        "openai analyse fehlgeschlagen", "error code: 401", "invalid_api_key",
        "credit_balance_exhausted", "insufficient_quota",
    ))
