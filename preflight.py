from __future__ import annotations

import re
from pathlib import Path


ZBON_STRONG_TERMS = ("tagesabschlussbericht", "z-berichtsnummer", "z-bericht", "tagesabschluss", "kassenabschluss")
ZBON_TERMS = (
    "z-bericht", "tagesabschluss", "kassenabschluss", "steuerübersicht",
    "steueruebersicht", "zahlungen", "stornierungen",
)


def extract_document_text(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                from pypdf import PdfReader
                chunks.extend(page.extract_text() or "" for page in PdfReader(str(path)).pages)
            elif suffix in {".xml", ".txt", ".csv"}:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            else:
                chunks.append(path.name)
        except Exception as exc:
            chunks.append(f"[Textauslese fehlgeschlagen fuer {path.name}: {exc}]")
    return "\n".join(chunks).lower()


def detect_zbon(paths: list[Path]) -> tuple[bool, str | None]:
    return detect_zbon_text(extract_document_text(paths))


def detect_zbon_text(text: str) -> tuple[bool, str | None]:
    text = text.lower()
    for term in ZBON_STRONG_TERMS:
        if term in text:
            return True, f"Z-Bon erkannt: {term}"
    matches = [term for term in ZBON_TERMS if term in text]
    if len(matches) >= 3 or ("zahlungen" in matches and "stornierungen" in matches):
        return True, f"Z-Bon erkannt anhand mehrerer Merkmale: {', '.join(matches)}"
    return False, None


def detect_non_booking_document(paths: list[Path]) -> tuple[bool, str | None, str | None]:
    text = extract_document_text(paths)
    if re.search(r"kontoauszug|kontobewegung|bank statement", text):
        return True, "kontoauszug", "Kontoauszug: IGNORIEREN / NICHT BUCHEN"
    if re.search(r"kartenzahlung|kartenbeleg|ec[- ]?zahlung|credit card", text):
        return True, "kartenzahlungsbeleg", "Kartenzahlungsbeleg: IGNORIEREN / NICHT BUCHEN"
    return False, None, None
