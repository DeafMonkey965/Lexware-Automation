from pathlib import Path
from unittest.mock import Mock

import pytest

from database import Database
from main import main, retry_openai
from models import AnalysisResult
from lexware_api import LexwareClient
from test_api import FakeResponse, FakeSession


def failure(voucher_id="retry", **kwargs):
    return AnalysisResult(lexware_voucher_id=voucher_id, file_id="file-1",
                          warnings=["OpenAI Analyse fehlgeschlagen: Error code: 401"], **kwargs)


@pytest.mark.parametrize("warning", ["OpenAI Analyse fehlgeschlagen: timeout", "Error code: 401", "invalid_api_key", r"invalid\_api\_key"])
def test_selection_only_failed_incomplete_non_zbon(tmp_path, warning):
    db = Database(tmp_path / "test.db")
    selected = failure()
    selected.warnings = [warning]
    db.save(selected)
    for status in ("GRUEN", "PRUEFEN", "SONDERFALL", "IGNORIEREN_ZBON"):
        db.save(failure(status, status=status))
    db.save(failure("zbon", document_type="zbon"))
    db.save(AnalysisResult(lexware_voucher_id="ordinary", warnings=["Rechnungsnummer fehlt"]))
    assert [r.lexware_voucher_id for r in db.openai_failures()] == ["retry"]


@pytest.mark.parametrize("named", [True, False])
def test_cache_reused_and_existing_row_updated(tmp_path, monkeypatch, capsys, named):
    monkeypatch.chdir(tmp_path)
    db = Database()
    old = failure(hints=["Dokument rechnung.xml konnte ohne KI nicht analysiert werden"] if named else [])
    db.save(old)
    db.save(AnalysisResult(lexware_voucher_id="untouched", status="GRUEN"))
    Path("cache").mkdir()
    path = Path("cache") / ("rechnung.xml" if named else "file-1.xml")
    path.write_text("Rechnung")
    client = Mock(spec=LexwareClient)
    analyzer = Mock()
    analyzer.analyze_files.return_value = AnalysisResult(lexware_voucher_id="retry", file_id="file-1", status="GRUEN", supplier="Neu")
    retry_openai(client, db, analyzer)
    assert client.mock_calls == []
    analyzer.analyze_files.assert_called_once_with([path], "retry", ["file-1"])
    assert len(db.all()) == 2
    assert next(r for r in db.all() if r.lexware_voucher_id == "retry").supplier == "Neu"
    assert db.openai_failures() == []
    output = capsys.readouterr().out
    assert "Erneut zu analysieren: 1" in output
    assert "1: GRUEN retry" in output
    assert "Erfolgreich neu analysiert: 1" in output
    assert "Weiterhin fehlerhaft: 0" in output


def test_missing_files_loaded_only_by_get(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lexware_api.time.sleep", lambda _: None)
    db = Database()
    old = failure()
    old.file_id = None
    db.save(old)
    session = FakeSession([FakeResponse(200, payload={"files": ["a", "b"]}),
                           FakeResponse(200, {"Content-Type": "application/xml"}),
                           FakeResponse(200, {"Content-Type": "application/xml"})])
    analyzer = Mock()
    analyzer.analyze_files.return_value = AnalysisResult(lexware_voucher_id="retry", file_id="a,b", status="PRUEFEN")
    retry_openai(LexwareClient("test", session=session), db, analyzer)
    assert [(m, u) for m, u, _ in session.calls] == [
        ("GET", "https://api.lexware.io/v1/vouchers/retry"),
        ("GET", "https://api.lexware.io/v1/files/a"),
        ("GET", "https://api.lexware.io/v1/files/b")]
    analyzer.analyze_files.assert_called_once_with([Path("cache/a.xml"), Path("cache/b.xml")], "retry", ["a", "b"])


@pytest.mark.parametrize("mode", ["api", "download", "missing_key", "exception"])
def test_failed_retry_preserves_row_and_continues(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.chdir(tmp_path)
    db = Database()
    for name in ("first", "second"):
        db.save(failure(name))
    before = {r.lexware_voucher_id: r.as_json() for r in db.all()}
    client = Mock(spec=LexwareClient)
    client.download_file.return_value = tmp_path / "file.xml"
    analyzer = Mock()
    analyzer.analyze_files.return_value = failure()
    if mode == "download":
        client.download_file.side_effect = RuntimeError("Download fehlgeschlagen")
    elif mode == "missing_key":
        analyzer.analyze_files.return_value.warnings = ["OPENAI_API_KEY fehlt oder Analyse nicht verfuegbar"]
    elif mode == "exception":
        analyzer.analyze_files.side_effect = RuntimeError("Analyse fehlgeschlagen")
    retry_openai(client, db, analyzer)
    assert {r.lexware_voucher_id: r.as_json() for r in db.all()} == before
    assert client.download_file.call_count == 2
    assert "Weiterhin fehlerhaft: 2" in capsys.readouterr().out


def test_empty_retry_does_nothing(tmp_path, capsys):
    client, analyzer = Mock(), Mock()
    retry_openai(client, Database(tmp_path / "empty.db"), analyzer)
    assert client.mock_calls == analyzer.mock_calls == []
    assert "Erneut zu analysieren: 0" in capsys.readouterr().out


def test_cli_dispatch(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py", "retry-openai"])
    monkeypatch.setattr("main.load_dotenv", Mock())
    monkeypatch.setenv("LEXWARE_API_KEY", "test")
    for name in ("Database", "LexwareClient", "DocumentAnalyzer"):
        monkeypatch.setattr(f"main.{name}", Mock())
    retry = Mock()
    monkeypatch.setattr("main.retry_openai", retry)
    main()
    retry.assert_called_once()
