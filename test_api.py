from pathlib import Path

import pytest

from analyzer import DocumentAnalyzer, build_fallback_analysis
from lexware_api import LexwareClient, LexwareWriteAttempt
from models import AnalysisResult, TaxGroup
from main import scan, vouchers_to_process
from preflight import detect_zbon_text
from validator import validate_analysis


class FakeResponse:
	def __init__(self, status_code, headers=None, payload=None):
		self.status_code = status_code
		self.headers = headers or {}
		self.content = b"file"
		self.payload = payload

	def raise_for_status(self):
		if self.status_code >= 400:
			raise RuntimeError(self.status_code)

	def json(self):
		return self.payload if self.payload is not None else {"content": [], "last": True}


class FakeSession:
	def __init__(self, responses):
		self.responses = iter(responses)
		self.headers = {}
		self.calls = []

	def request(self, method, url, **kwargs):
		self.calls.append((method, url, kwargs))
		return next(self.responses)


def test_lexware_write_methods_are_blocked_without_network_call():
	client = LexwareClient("test-token")
	for method in ("POST", "PUT", "PATCH", "DELETE"):
		with pytest.raises(LexwareWriteAttempt):
			client._request(method, "/v1/profile")


def test_429_retries_with_exponential_delays(monkeypatch):
	session = FakeSession([FakeResponse(429), FakeResponse(429), FakeResponse(200)])
	client = LexwareClient("test-token", session=session)
	sleeps = []
	monkeypatch.setattr("lexware_api.time.sleep", sleeps.append)
	monkeypatch.setattr("lexware_api.time.monotonic", lambda: 100.0)

	client._request("GET", "/v1/profile")

	assert [call[0] for call in session.calls] == ["GET", "GET", "GET"]
	assert sleeps == [2.0, 0.7, 4.0, 0.7]


def test_retry_after_header_is_preferred(monkeypatch):
	session = FakeSession([FakeResponse(429, {"Retry-After": "5"}), FakeResponse(200)])
	client = LexwareClient("test-token", session=session)
	sleeps = []
	monkeypatch.setattr("lexware_api.time.sleep", sleeps.append)
	monkeypatch.setattr("lexware_api.time.monotonic", lambda: 100.0)

	client._request("GET", "/v1/profile")

	assert sleeps == [5.0, 0.7]


def test_download_file_uses_content_disposition_extension(tmp_path):
	response = FakeResponse(200, {
		"Content-Type": "application/pdf",
		"Content-Disposition": 'attachment; filename="lieferant-rechnung.pdf"',
	})
	client = LexwareClient("test-token", session=FakeSession([response]))

	path = client.download_file("file-1", tmp_path / "file-1.bin")

	assert path.name == "lieferant-rechnung.pdf"
	assert path.read_bytes() == b"file"


def test_download_file_uses_mime_extension_without_filename(tmp_path):
	response = FakeResponse(200, {"Content-Type": "image/png"})
	client = LexwareClient("test-token", session=FakeSession([response]))

	path = client.download_file("file-2", tmp_path / "file-2.bin")

	assert path.name == "file-2.png"


def test_analyzer_receives_multiple_files_as_one_request():
	analyzer = DocumentAnalyzer(None)
	result = analyzer.analyze_files([Path("rechnung.pdf"), Path("rechnung.xml")], "voucher-2", ["pdf-1", "xml-1"])

	assert result.file_id == "pdf-1,xml-1"
	assert "rechnung.pdf" in result.hints[0]
	assert "rechnung.xml" in result.hints[0]


def test_analysis_supports_multiple_tax_groups_and_validates_math():
	result = AnalysisResult(
		supplier="Testlieferant",
		invoice_number="RE-1",
		date="2026-01-15",
		document_type="Eingangsrechnung",
		total_gross=130.00,
		tax_groups=[
			TaxGroup(rate=7, net=50.00, tax=3.50, gross=53.50),
			TaxGroup(rate=19, net=64.29, tax=12.21, gross=76.50),
		],
		category="Wareneinkauf",
	)
	validated = validate_analysis(result, "voucher-1")
	assert validated.status == "GRUEN"
	assert len(validated.tax_groups) == 2


def test_missing_values_are_null_and_warned():
	result = build_fallback_analysis("unknown.pdf")
	assert result.supplier is None
	assert result.status == "UNVOLLSTAENDIG"
	assert result.warnings


def test_unchecked_vouchers_single_page_uses_official_paging_params():
	payload = {"content": [{"id": "voucher-1"}], "last": True, "totalPages": 1, "totalElements": 1, "number": 0}
	session = FakeSession([FakeResponse(200, payload=payload)])
	client = LexwareClient("test-token", session=session)

	assert client.unchecked_vouchers() == [{"id": "voucher-1"}]
	assert session.calls[0][0] == "GET"
	assert session.calls[0][2]["params"] == {"voucherType": "any", "voucherStatus": "unchecked", "page": 0, "size": 250}


def test_unchecked_vouchers_two_pages_are_combined():
	responses = [
		FakeResponse(200, payload={"content": [{"id": "voucher-1"}], "last": False, "totalPages": 2, "totalElements": 2, "number": 0}),
		FakeResponse(200, payload={"content": [{"id": "voucher-2"}], "last": True, "totalPages": 2, "totalElements": 2, "number": 1}),
	]
	session = FakeSession(responses)
	client = LexwareClient("test-token", session=session)

	assert client.unchecked_vouchers() == [{"id": "voucher-1"}, {"id": "voucher-2"}]
	assert len(session.calls) == 2


def test_unchecked_vouchers_returns_all_227_elements_with_page_size_250():
	content = [{"id": f"voucher-{index}"} for index in range(227)]
	payload = {"content": content, "last": True, "totalPages": 1, "totalElements": 227, "number": 0}
	session = FakeSession([FakeResponse(200, payload=payload)])
	client = LexwareClient("test-token", session=session)

	result = client.unchecked_vouchers()

	assert len(result) == 227
	assert result[-1]["id"] == "voucher-226"


def test_tagesabschlussbericht_is_ignored_zbon():
	detected, warning = detect_zbon_text("Tagesabschlussbericht Zahlungen Stornierungen")

	assert detected is True
	assert "Z-Bon" in warning


def test_z_berichtsnummer_is_ignored_zbon():
	detected, warning = detect_zbon_text("Z-Berichtsnummer: 713")

	assert detected is True
	assert "Z-Bon" in warning


def test_normal_invoice_is_not_zbon():
	detected, warning = detect_zbon_text("Eingangsrechnung Rechnungsnummer RE-713")

	assert detected is False
	assert warning is None


def test_saved_green_voucher_is_skipped_without_force(tmp_path):
	from database import Database

	database = Database(tmp_path / "buchhaltung.db")
	database.save(AnalysisResult(lexware_voucher_id="green", status="GRUEN"))

	selected, known, new = vouchers_to_process([{"id": "green"}, {"id": "new"}], database, None, False)

	assert [voucher["id"] for voucher in selected] == ["new"]
	assert (known, new) == (1, 1)


def test_saved_zbon_is_skipped_without_force(tmp_path):
	from database import Database

	database = Database(tmp_path / "buchhaltung.db")
	database.save(AnalysisResult(lexware_voucher_id="zbon", status="IGNORIEREN_ZBON", document_type="zbon"))

	selected, _, _ = vouchers_to_process([{"id": "zbon"}], database, None, False)

	assert selected == []


def test_all_saved_statuses_are_skipped_without_force(tmp_path):
	from database import Database

	database = Database(tmp_path / "buchhaltung.db")
	statuses = ("GRUEN", "PRUEFEN", "UNVOLLSTAENDIG", "SONDERFALL", "IGNORIEREN_ZBON", "IGNORIEREN_KONTOAUSZUG", "IGNORIEREN_KARTENBELEG")
	for index, status in enumerate(statuses):
		database.save(AnalysisResult(lexware_voucher_id=f"saved-{index}", status=status))

	selected, known, new = vouchers_to_process([{"id": f"saved-{index}"} for index in range(len(statuses))] + [{"id": "new"}], database, 10, False)

	assert [voucher["id"] for voucher in selected] == ["new"]
	assert (known, new) == (len(statuses), 1)


def test_force_reprocesses_saved_voucher(tmp_path):
	from database import Database

	database = Database(tmp_path / "buchhaltung.db")
	database.save(AnalysisResult(lexware_voucher_id="known", status="GRUEN"))

	selected, _, _ = vouchers_to_process([{"id": "known"}], database, None, True)

	assert [voucher["id"] for voucher in selected] == ["known"]


def test_limit_counts_only_unknown_vouchers(tmp_path):
	from database import Database

	database = Database(tmp_path / "buchhaltung.db")
	for voucher_id in ("known-1", "known-2"):
		database.save(AnalysisResult(lexware_voucher_id=voucher_id, status="UNVOLLSTAENDIG"))

	vouchers = [{"id": "known-1"}, {"id": "known-2"}] + [{"id": f"new-{index}"} for index in range(12)]
	selected, known, new = vouchers_to_process(vouchers, database, 10, False)

	assert [voucher["id"] for voucher in selected] == [f"new-{index}" for index in range(10)]
	assert (known, new) == (2, 12)


def test_scan_limit_counts_only_relevant_belege(tmp_path, monkeypatch):
	from database import Database

	class FakeClient:
		def unchecked_vouchers(self):
			return [{"id": f"zbon-{index}"} for index in range(15)] + [{"id": f"invoice-{index}"} for index in range(10)]

		def voucher(self, voucher_id):
			if voucher_id.startswith("zbon-"):
				return {"id": voucher_id, "remark": "Tagesabschlussbericht"}
			return {"id": voucher_id, "files": []}

	class FakeAnalyzer:
		def __init__(self):
			self.calls = []

		def analyze(self, path, voucher_id, file_id=None):
			self.calls.append(voucher_id)
			return AnalysisResult(lexware_voucher_id=voucher_id, status="GRUEN")

	monkeypatch.chdir(tmp_path)
	database = Database(tmp_path / "buchhaltung.db")
	analyzer = FakeAnalyzer()
	scan(FakeClient(), database, 10, False, analyzer)

	assert analyzer.calls == [f"invoice-{index}" for index in range(10)]
	assert len(database.all()) == 25


def test_no_lexware_write_call_names_in_project_code():
	root = Path(__file__).parent
	forbidden = ("requests.post", "requests.put", "requests.patch", "requests.delete")
	for path in root.glob("*.py"):
		if path.name == "test_api.py":
			continue
		text = path.read_text(encoding="utf-8")
		assert not any(token in text for token in forbidden)
