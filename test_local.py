import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import pymupdf
from PIL import Image

from analyzer import DocumentAnalyzer, OpenAIAnalyzer
from database import Database
from exporter import export_results
from lexware_api import LexwareClient, LexwareWriteAttempt
from local_config import LocalConfig
from local_extractor import LocalDocumentExtractor
from local_rules import extract_rules, amount
from main import retry_local, scan
from models import AnalysisResult
from ollama_client import OllamaClient
from test_api import FakeResponse, FakeSession
from validator import validate_analysis


def invoice(rate=19, net="100,00", tax="19,00", gross="119,00"):
    return f"""Rechnung
Lieferant: Muster GmbH
Rechnungsnummer: R-2026-123
Rechnungsdatum: 09.09.2026
Faellig am: 23.09.2026
Gesamt Netto: {net}
Gesamt Umsatzsteuer: {tax}
Gesamt Brutto: {gross} EUR
{rate} % Netto {net} Steuer {tax} Brutto {gross}
"""


def analyze_text(tmp_path, text, config=None):
    path = tmp_path / "test.txt"
    path.write_text(text, encoding="utf-8")
    return DocumentAnalyzer(config=config or LocalConfig()).analyze(path, "v", "f")


@pytest.mark.parametrize("marker", ["Tagesabschlussbericht", "Z-Berichtsnummer", "Z-Bericht", "Tagesabschluss", "Kassenabschluss"])
def test_zbon_never_calls_ai(tmp_path, monkeypatch, marker):
    monkeypatch.setattr("ollama_client.OllamaClient.analyze", Mock(side_effect=AssertionError("KI verboten")))
    remote = Mock(side_effect=AssertionError("OpenAI verboten"))
    monkeypatch.setattr("analyzer.OpenAIAnalyzer", remote)
    result = analyze_text(tmp_path, marker, LocalConfig(True, True, ollama_model="local"))
    assert result.status == "IGNORIEREN_ZBON"
    assert not remote.called


def test_digital_pdf_all_pages_without_ocr(tmp_path, monkeypatch):
    path = tmp_path / "digital.pdf"
    with pymupdf.open() as doc:
        for text in (invoice(), "Zweite Seite mit weiteren eindeutig lesbaren Rechnungsdaten"):
            page = doc.new_page()
            page.insert_text((30, 30), text)
        doc.save(path)
    extractor = LocalDocumentExtractor()
    ocr = Mock(side_effect=AssertionError("OCR unnötig"))
    monkeypatch.setattr(extractor, "ocr_image", ocr)
    result = extractor.extract(path)
    assert "Muster GmbH" in result.text and "Zweite Seite" in result.text
    assert not result.used_ocr and not ocr.called


def test_mixed_pdf_ocr_only_image_page(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 30), invoice())
        doc.new_page(width=144, height=144)
        doc.new_page(width=144, height=144)
        doc.save(path)
    extractor = LocalDocumentExtractor()
    sizes = []
    def ocr(image):
        sizes.append(image.size)
        return f"OCR Seite {len(sizes)}"
    monkeypatch.setattr(extractor, "ocr_image", ocr)
    result = extractor.extract(path)
    assert sizes == [(600, 600), (600, 600)]
    assert result.used_ocr and "OCR Seite 2" in result.text and "Muster" in result.text


def test_multiframe_tiff_and_debug_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "multi.tiff"
    Image.new("RGB", (20, 20)).save(path, save_all=True, append_images=[Image.new("RGB", (20, 20))])
    extractor = LocalDocumentExtractor(save_ocr_text=True)
    monkeypatch.setattr(extractor, "ocr_image", Mock(side_effect=["Seite eins", "Seite zwei"]))
    result = extractor.extract(path, "file-1")
    assert result.text == "Seite eins\nSeite zwei"
    assert Path("cache/ocr/file-1.txt").read_text() == result.text


@pytest.mark.parametrize("languages, expected", [(["deu", "eng", "osd"], "deu+eng"), (["eng"], "eng"), (["fra"], "fra")])
def test_ocr_language_selection(monkeypatch, languages, expected):
    monkeypatch.setattr("local_extractor.find_tesseract", lambda: "test.exe")
    monkeypatch.setattr("pytesseract.get_languages", lambda config: languages)
    ocr = Mock(return_value="Text")
    monkeypatch.setattr("pytesseract.image_to_string", ocr)
    LocalDocumentExtractor().ocr_image(Image.new('RGB', (100, 100), 'white'))
    assert ocr.call_args.kwargs["lang"] == expected


def test_missing_tesseract_is_warning(tmp_path, monkeypatch):
    path = tmp_path / "scan.png"
    Image.new("RGB", (20, 20)).save(path)
    monkeypatch.setattr("local_extractor.find_tesseract", lambda: None)
    result = DocumentAnalyzer(config=LocalConfig()).analyze(path, "v")
    assert result.status == "UNVOLLSTAENDIG" and result.analysis_method == "ocr_rules"
    assert any("Tesseract nicht gefunden" in w for w in result.warnings)


@pytest.mark.parametrize("rate,tax,gross", [(19, "19,00", "119,00"), (7, "7,00", "107,00")])
def test_normal_invoice(tmp_path, rate, tax, gross):
    result = analyze_text(tmp_path, invoice(rate=rate, tax=tax, gross=gross))
    assert result.status == "GRUEN"
    assert result.tax_groups[0].rate == rate
    assert result.total_net == 100 and result.total_tax == amount(tax)
    assert result.date == "2026-09-09" and result.due_date == "2026-09-23"
    assert result.supplier == "Muster GmbH" and result.currency == "EUR"


def test_mixed_tax_table(tmp_path):
    text = invoice().split("Gesamt Netto")[0] + """Gesamt Brutto: 226,00 EUR
Gesamt Netto: 200,00
Gesamt Umsatzsteuer: 26,00
Satz Netto MwSt Brutto
7 % 100,00 7,00 107,00
19 % 100,00 19,00 119,00
"""
    result = analyze_text(tmp_path, text)
    assert result.status == "GRUEN"
    assert [g.gross for g in result.tax_groups] == [107, 119]


@pytest.mark.parametrize("gross,status", [("119,01", "GRUEN"), ("120,00", "PRUEFEN")])
def test_rounding_and_math(tmp_path, gross, status):
    result = analyze_text(tmp_path, invoice(gross=gross))
    assert result.status == status


def test_unclear_invoice_does_not_invent_values(tmp_path):
    result = analyze_text(tmp_path, "Rechnung\nEmpfaenger: Kunde GmbH\n123,45\n7 % 19 %")
    assert result.supplier is result.date is result.total_gross is None
    assert all(g.net is g.tax is g.gross is None for g in result.tax_groups)
    assert result.status == "UNVOLLSTAENDIG" and result.warnings


def test_conflicting_totals_and_special_items(tmp_path):
    result = analyze_text(tmp_path, invoice() + "Gesamt Brutto: 120,00 EUR\nPfand: 5,00\nPalettenmiete 3,00\n")
    assert result.total_gross is None
    assert len(result.special_items) == 2


def test_supplier_registry_requires_all_identity_markers(tmp_path):
    registry = tmp_path / "rules.json"
    registry.write_text(json.dumps([{"name": "Lieferant GmbH", "markers": ["DE123456789", "Lieferstrasse 1"]}]))
    assert extract_rules("DE123456789", registry).supplier is None
    assert extract_rules("DE123456789 Lieferstrasse 1", registry).supplier == "Lieferant GmbH"


def settlement(payout="200,20"):
    return invoice() + f"""Wolt Abrechnung
Abrechnungszeitraum 01.09.2026 bis 07.09.2026
Umsatz 7 % Brutto: 107,00
Umsatz 19 % Brutto: 119,00
Trinkgeld: 10,00
Provision Netto: 20,00
Provision USt: 3,80
Gebuehren Netto: 10,00
Gebuehren USt: 1,90
Gutschriften: 0,00
Korrekturen: 0,00
Erstattungen: 0,00
Sonstige Positionen: -0,10
Auszahlung: {payout}
"""


@pytest.mark.parametrize("payout,status,difference", [("200,20", "GRUEN", 0), ("205,20", "PRUEFEN", 5)])
def test_platform_payout(tmp_path, payout, status, difference):
    result = analyze_text(tmp_path, settlement(payout))
    assert result.document_type == "plattformabrechnung"
    assert result.platform.platform == "wolt"
    assert result.platform.settlement_period_from == "2026-09-01"
    assert result.platform.settlement_period_to == "2026-09-07"
    assert result.platform.expected_payout == 200.20
    assert result.platform.difference == difference
    assert result.status == status


def test_platform_missing_values_not_assumed_zero(tmp_path):
    result = analyze_text(tmp_path, settlement().replace("Trinkgeld: 10,00", ""))
    assert result.platform.tips is None and result.platform.expected_payout is None
    assert result.status != "GRUEN"


def test_openai_default_false_even_with_key(tmp_path, monkeypatch):
    monkeypatch.delenv("USE_OPENAI_FALLBACK", raising=False)
    monkeypatch.delenv("USE_OLLAMA", raising=False)
    remote = Mock(side_effect=AssertionError("OpenAI darf nicht initialisiert werden"))
    monkeypatch.setattr("openai.OpenAI", remote)
    path = tmp_path / "unknown.txt"
    path.write_text("Rechnung unklar")
    analyzer = DocumentAnalyzer("present-key")
    analyzer.analyze(path, "v")
    assert analyzer.openai_calls == 0 and not remote.called
    assert analyzer.config.use_openai_fallback is False


def test_ollama_failure_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("ollama_client.OllamaClient.analyze", Mock(side_effect=ConnectionError()))
    result = analyze_text(tmp_path, "Rechnung unklar", LocalConfig(use_ollama=True, ollama_model="local"))
    assert "Lokale KI nicht verfügbar" in result.warnings
    assert result.status == "UNVOLLSTAENDIG"


def test_complete_rules_skip_both_models(tmp_path, monkeypatch):
    remote = Mock(side_effect=AssertionError("unnötig"))
    local = Mock(side_effect=AssertionError("unnötig"))
    monkeypatch.setattr("analyzer.OpenAIAnalyzer", remote)
    monkeypatch.setattr("ollama_client.OllamaClient.analyze", local)
    assert analyze_text(tmp_path, invoice(), LocalConfig(True, True)).status == "GRUEN"
    assert not remote.called and not local.called


@pytest.mark.parametrize("url", ["https://api.lexware.io", "http://external.example", "http://localhost@external.example"])
def test_ollama_only_loopback(url):
    with pytest.raises(ValueError):
        OllamaClient(url, "local")


def test_ollama_rejects_invalid_json(monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=None)
    session.request.return_value.status_code = 200
    session.request.return_value.json.return_value = {"response": '{"total_gross":"invented", "extra": true}'}
    monkeypatch.setattr("ollama_client.requests.Session", lambda: session)
    with pytest.raises(ValueError):
        OllamaClient("http://localhost:11434", "local").analyze("text")
    assert session.request.call_args.kwargs["allow_redirects"] is False


def test_openai_terminal_error_stops_further_calls(tmp_path, monkeypatch):
    remote = Mock()
    remote.return_value.responses.create.side_effect = RuntimeError("credit_balance_exhausted")
    monkeypatch.setattr("openai.OpenAI", remote)
    path = tmp_path / "file.png"
    Image.new("RGB", (20, 20)).save(path)
    analyzer = OpenAIAnalyzer("test")
    analyzer.analyze(path, "v1")
    analyzer.analyze(path, "v2")
    assert remote.return_value.responses.create.call_count == 1
    assert remote.call_args.kwargs["max_retries"] == 0


def test_retry_local_updates_only_old_api_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USE_OPENAI_FALLBACK", "true")
    remote = Mock(side_effect=AssertionError("OpenAI verboten"))
    monkeypatch.setattr("analyzer.OpenAIAnalyzer", remote)
    Path("cache").mkdir()
    Path("cache/original.xml").write_text("<text>" + invoice() + "</text>", encoding="utf-8")
    db = Database()
    db.save(AnalysisResult(lexware_voucher_id="retry", file_id="f", warnings=["credit_balance_exhausted"],
                           hints=["Dokument original.xml konnte ohne KI nicht analysiert werden"]))
    db.save(AnalysisResult(lexware_voucher_id="zbon", document_type="zbon", warnings=["invalid_api_key"]))
    db.save(AnalysisResult(lexware_voucher_id="good", status="GRUEN"))
    db.save(AnalysisResult(lexware_voucher_id="ordinary", warnings=["Datum fehlt"]))
    client = Mock(spec=LexwareClient)
    retry_local(client, db)
    assert not client.mock_calls and not remote.called
    assert db.get("retry").status == "GRUEN"
    assert db.get("retry").analysis_method == "rules" and len(db.all()) == 4
    assert "Lokal erfolgreich analysiert: 1" in capsys.readouterr().out


def test_retry_local_persists_incomplete_local_results(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("cache").mkdir()
    Path("cache/f.xml").write_text("<text>Unklar</text>")
    db = Database()
    db.save(AnalysisResult(lexware_voucher_id="v", file_id="f", warnings=["invalid_api_key"]))
    retry_local(Mock(), db, DocumentAnalyzer(config=LocalConfig()))
    assert db.get("v").status == "UNVOLLSTAENDIG"
    assert db.get("v").analysis_method == "rules" and db.openai_failures() == []
    assert "Weiterhin zu prüfen: 1" in capsys.readouterr().out


def test_download_cache_reuses_named_file(tmp_path):
    session = FakeSession([FakeResponse(200, {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="named.pdf"'})])
    client = LexwareClient("test", session=session)
    first = client.download_file("f", tmp_path / "f")
    assert client.download_file("f", tmp_path / "f") == first
    assert len(session.calls) == 1


def test_export_shows_fields_and_escapes_html(tmp_path):
    result = validate_analysis(extract_rules(settlement()), "<voucher>")
    result.confidence = 0.95
    export_results([result], tmp_path)
    output = (tmp_path / "pruefung.html").read_text(encoding="utf-8")
    for label in ("Voucher-ID", "Dokumenttyp", "Analyse-Methode", "Confidence", "Trinkgeld", "Kontrolldifferenz"):
        assert label in output
    assert "&lt;voucher&gt;" in output and "<voucher>" not in output


def test_get_only_also_blocks_other_methods():
    session = FakeSession([])
    client = LexwareClient("test", session=session)
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        with pytest.raises(LexwareWriteAttempt):
            client._request(method, "/v1/profile")
    assert not session.calls


def test_lexware_absolute_url_cannot_bypass_get_guard():
    session = FakeSession([])
    client = LexwareClient("test", base_url="http://localhost", session=session)
    with pytest.raises(LexwareWriteAttempt):
        client._request("POST", "https://api.lexware.io/v1/vouchers")
    assert not session.calls


def test_scan_limit_ignores_ocr_zbons_and_reports_zero_openai(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    paths = []
    for i, content in enumerate(("Tagesabschlussbericht", invoice(), invoice())):
        path = tmp_path / f"{i}.txt"
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    client = Mock(spec=LexwareClient)
    client.unchecked_vouchers.return_value = [{"id": str(i)} for i in range(3)]
    client.voucher.side_effect = lambda vid: {"files": [vid]}
    client.download_file.side_effect = lambda fid, target: paths[int(fid)]
    db = Database()
    scan(client, db, 1, False, DocumentAnalyzer(config=LocalConfig()))
    assert db.get("0").status == "IGNORIEREN_ZBON"
    assert db.get("1").status == "GRUEN" and db.get("2") is None
    output = capsys.readouterr().out
    assert "Lokal analysiert: 1" in output and "OpenAI verwendet: 0" in output


def test_partial_download_cannot_be_green(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "first.txt"
    path.write_text(invoice(), encoding="utf-8")
    client = Mock(spec=LexwareClient)
    client.unchecked_vouchers.return_value = [{"id": "v"}]
    client.voucher.return_value = {"files": ["a", "b"]}
    client.download_file.side_effect = [path, RuntimeError("Download fehlt")]
    db = Database()
    scan(client, db, 1, False, DocumentAnalyzer(config=LocalConfig()))
    assert db.get("v").status == "UNVOLLSTAENDIG" and db.get("v").file_id == "a,b"


def test_ollama_fills_evidenced_fields_and_skips_openai(tmp_path, monkeypatch):
    text = invoice().replace("Lieferant: Muster GmbH", "Muster GmbH")
    monkeypatch.setattr("ollama_client.OllamaClient.analyze", lambda self, text: AnalysisResult(supplier="Muster GmbH"))
    remote = Mock(side_effect=AssertionError("Lokale Extraktion ausreichend"))
    monkeypatch.setattr("analyzer.OpenAIAnalyzer", remote)
    result = analyze_text(tmp_path, text, LocalConfig(True, True, ollama_model="local"))
    assert result.supplier == "Muster GmbH" and result.analysis_method == "rules+ollama"
    assert result.status == "PRUEFEN" and not remote.called


def test_ollama_invented_value_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("ollama_client.OllamaClient.analyze", lambda self, text: AnalysisResult(supplier="Erfunden GmbH", total_gross=999))
    result = analyze_text(tmp_path, "Unklare Rechnung", LocalConfig(use_ollama=True, ollama_model="local"))
    assert result.supplier is None and result.total_gross is None
    assert any("nicht im Dokument belegt" in w for w in result.warnings)


def test_explicit_openai_fallback_only_after_local_failure(tmp_path, monkeypatch):
    fake = Mock()
    fake.calls = 1
    fake.analyze_files.return_value = AnalysisResult(supplier="Test", total_gross=100)
    factory = Mock(return_value=fake)
    monkeypatch.setattr("analyzer.OpenAIAnalyzer", factory)
    result = analyze_text(tmp_path, "Unklare Rechnung", LocalConfig(use_openai_fallback=True))
    factory.assert_called_once()
    assert result.analysis_method == "rules+openai" and result.status != "GRUEN"


def test_multifile_analysis_combines_invoice_pages(tmp_path):
    first, second = tmp_path / "one.txt", tmp_path / "two.txt"
    header, totals = invoice().split("Gesamt Netto:")
    first.write_text(header, encoding="utf-8")
    second.write_text("Gesamt Netto:" + totals, encoding="utf-8")
    result = DocumentAnalyzer(config=LocalConfig()).analyze_files([first, second], "v", ["a", "b"])
    assert result.status == "GRUEN" and result.file_id == "a,b"


def test_old_database_json_remains_readable(tmp_path):
    db = Database(tmp_path / "test.db")
    old = AnalysisResult(lexware_voucher_id="old", status="GRUEN")
    db.save(old)
    db.connection.execute("UPDATE analyses SET analyse_json = ?", (json.dumps({"lexware_voucher_id": "old", "status": "GRUEN"}),))
    db.connection.commit()
    loaded = db.get("old")
    assert loaded.status == "GRUEN" and loaded.analysis_method is None and loaded.tax_groups == []


def test_missing_retry_file_get_only_and_preserve_on_download_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database()
    db.save(AnalysisResult(lexware_voucher_id="v", file_id="f", warnings=["invalid_api_key"]))
    response = FakeResponse(200, {"Content-Type": "application/xml"})
    response.content = ("<text>" + invoice() + "</text>").encode()
    session = FakeSession([response])
    retry_local(LexwareClient("test", session=session), db, DocumentAnalyzer(config=LocalConfig()))
    assert db.get("v").status == "GRUEN"
    assert [(m, u) for m, u, _ in session.calls] == [("GET", "https://api.lexware.io/v1/files/f")]
