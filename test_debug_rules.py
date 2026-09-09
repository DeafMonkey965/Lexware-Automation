from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from local_rules import extract_rules, invoice_number_candidates, debug_candidates
from local_extractor import LocalDocumentExtractor


@pytest.mark.parametrize('word', ['Datum', 'Rechnung', 'Rechnungsdatum', 'Kundennummer',
                                 'Kunde', 'Seite', 'Beleg', 'MwSt', 'Netto', 'Brutto'])
@pytest.mark.parametrize('separator', [': ', '\n', ':\n'])
def test_field_labels_never_invoice_numbers(word, separator):
    text = f'Rechnungsnummer{separator}{word}\n12345'
    assert extract_rules(text).invoice_number is None
    assert invoice_number_candidates(text)[0]['accepted'] is False


@pytest.mark.parametrize('marker', ['Rechnungsnummer', 'Rechnungs-Nr.', 'Rechnung Nr.',
                                  'Re.-Nr.', 'Belegnummer', 'Invoice No.', 'Gutschriftsnummer'])
@pytest.mark.parametrize('separator', [': ', '\n'])
def test_explicit_invoice_markers(marker, separator):
    assert extract_rules(f'{marker}{separator}R-2026/123').invoice_number == 'R-2026/123'


@pytest.mark.parametrize('text', ['Rechnung 12345', 'Kundennummer: 12345', '12345',
                                 'Rechnungsnummer\n\n12345', 'Rechnungsnummer: 09.09.2026',
                                 'Rechnungsnummer: 10.03.26', 'Rechnungsnummer: 2026-09-09',
                                 'Rechnungsnummer: 119,00', 'Rechnungsnummer: Datum: 12345',
                                 'Rechnungsnummer: ABC', 'Rechnungsnummer: R-1\nBelegnummer: R-2'])
def test_missing_or_ambiguous_number_stays_null(text):
    assert extract_rules(text).invoice_number is None


def test_repeated_number_is_unambiguous():
    assert extract_rules('Rechnungsnummer: R-1\nRechnungsnummer: R-1').invoice_number == 'R-1'


def test_diagnostics_short_date_and_product_percentages():
    candidates = debug_candidates('10.03.26 15:37:59\nKaese 45% Fett\nMwSt 19,00 %\nBrutto 119,00')
    assert [c['value'] for c in candidates['Datum']] == ['10.03.26']
    assert [c['value'] for c in candidates['Geldbetraege']] == ['119,00']
    assert [c['value'] for c in candidates['Steuersaetze']] == ['19,00 %']


def test_diagnostic_candidates_share_parser_and_include_context(tmp_path):
    registry = tmp_path / 'rules.json'
    registry.write_text('[{"name":"Firma GmbH","markers":["DE123456789"]}]')
    text = ('Rechnungsnummer: Datum\nRechnungs-Nr.: R-123\nRechnungsdatum: 09.09.2026\n'
            'Gesamt Brutto: 119,00 EUR\nMwSt 19 %\nLieferant: Firma GmbH\nDE123456789')
    candidates = debug_candidates(text, registry)
    assert [c['value'] for c in candidates['Rechnungsnummer'] if c['accepted']] == ['R-123']
    assert candidates['Datum'][0]['value'] == '09.09.2026'
    assert candidates['Geldbetraege'][0]['context'] == 'Gesamt Brutto: 119,00 EUR'
    assert candidates['Steuersaetze'][0]['value'] == '19 %'
    assert len(candidates['Lieferanten']) == 2
    assert extract_rules(text, registry).supplier == 'Firma GmbH'


def test_ocr_short_date_and_noisy_labeled_totals():
    text = '''Rechnung
Lieferant: Firma GmbH
Rechnungsnummer: R-2026-10
Rechnungsdatum | 10.03.26
Gesamt Netto .... EUR 100,00
Gesamt Umsatzsteuer | 19,00 EUR
Gesamt Brutto .... 119,00 EUR
Satz Netto MwSt Brutto
19,00 % 100,00 19,00 119,00
'''
    result = extract_rules(text)
    assert result.date == '2026-03-10'
    assert result.total_net == 100.0
    assert result.total_tax == 19.0
    assert result.total_gross == 119.0
    assert len(result.tax_groups) == 1
    assert result.tax_groups[0].rate == 19
    assert result.tax_groups[0].net == 100.0
    assert result.tax_groups[0].tax == 19.0
    assert result.tax_groups[0].gross == 119.0


def test_decimal_mixed_tax_rates_are_not_counted_as_money():
    text = '''Rechnung
Lieferant: Firma GmbH
Rechnungsnummer: R-MIX
Rechnungsdatum: 09.09.2026
Gesamt Netto: 200,00
Gesamt Umsatzsteuer: 26,00
Gesamt Brutto: 226,00 EUR
Satz Netto MwSt Brutto
7,00 % 100,00 7,00 107,00
19,00 % 100,00 19,00 119,00
'''
    result = extract_rules(text)
    assert [(group.rate, group.net, group.tax, group.gross) for group in result.tax_groups] == [
        (7, 100.0, 7.0, 107.0),
        (19, 100.0, 19.0, 119.0),
    ]


def test_labeled_total_with_two_money_values_is_not_guessed():
    result = extract_rules('Rechnung\nGesamt Brutto 119,00 EUR inkl. 19,00 MwSt\n')
    assert result.total_gross is None
    assert any('Gesamt Brutto Zeile enthaelt mehrere Betraege' in warning for warning in result.warnings)


def test_tiff_page_texts_preserve_failed_page(tmp_path, monkeypatch):
    path = tmp_path / 'pages.tiff'
    Image.new('RGB', (20, 20)).save(path, save_all=True,
                                   append_images=[Image.new('RGB', (20, 20)) for _ in range(2)])
    monkeypatch.setattr(LocalDocumentExtractor, 'ocr_image', Mock(side_effect=['first', RuntimeError('fail'), 'third']))
    doc = LocalDocumentExtractor().extract(path)
    assert doc.page_texts == ['first', '', 'third']


def test_debug_full_text_all_pages_and_read_only(tmp_path, monkeypatch, capsys):
    from database import Database
    from models import AnalysisResult
    from main import debug_voucher
    monkeypatch.chdir(tmp_path)
    Path('cache').mkdir()
    path = Path('cache/file.tiff')
    Image.new('RGB', (20, 20)).save(path, save_all=True, append_images=[Image.new('RGB', (20, 20))])
    pages = ['Rechnungsnummer: Datum\n' + 'Langer Rohtext Ä €\n' * 2000, 'Zweite Seite ENDE']
    monkeypatch.setattr(LocalDocumentExtractor, 'ocr_image', Mock(side_effect=pages))
    database = Database()
    database.save(AnalysisResult(lexware_voucher_id='voucher', file_id='file'))
    database.connection.close()
    before = Path('buchhaltung.db').read_bytes()
    forbidden = Mock(side_effect=AssertionError('No network or AI'))
    monkeypatch.setattr('lexware_api.LexwareClient._request', forbidden)
    monkeypatch.setattr('analyzer.OpenAIAnalyzer.analyze_files', forbidden)
    monkeypatch.setattr('ollama_client.OllamaClient.analyze', forbidden)
    debug_voucher('voucher')
    output = capsys.readouterr().out
    saved = Path('debug/text/voucher.txt').read_text(encoding='utf-8')
    for index, page in enumerate(pages, 1):
        assert f'===== OCR ROHTEXT SEITE {index} =====\n{page}' in saved
        assert page in output
    assert '"invoice_number": null' in output
    assert 'Feldbezeichnung statt Rechnungsnummer' in output
    assert Path('buchhaltung.db').read_bytes() == before
    forbidden.assert_not_called()
