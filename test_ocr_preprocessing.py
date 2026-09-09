from pathlib import Path
from unittest.mock import Mock

import pytest
import pymupdf
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageStat

from local_extractor import LocalDocumentExtractor, find_tesseract
from ocr_preprocessing import prepare, perspective, quality_score


def receipt():
    image = Image.new('RGB', (1200, 1600), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 30)
    lines = ['Rechnung Muster Lieferant GmbH', 'Rechnungsdatum 09.09.2026',
             'Rechnungsnummer R-2026-123', 'Netto 100,00 EUR MwSt 19,00 EUR',
             'Brutto 119,00 EUR Vielen Dank'] * 5
    for i, line in enumerate(lines):
        draw.text((70, 50+i*55), line, fill='black', font=font)
    return image


@pytest.mark.parametrize('angle', [90, 180, 270])
def test_real_rotated_receipt(angle):
    import pytesseract
    assert find_tesseract(), 'Tesseract required for OCR integration tests'
    extractor = LocalDocumentExtractor()
    text = extractor.ocr_image(receipt().rotate(angle, expand=True))
    assert '119,00' in text and 'Rechnung' in text
    assert extractor._debug[0]['rotation'] == angle


def test_dark_and_small():
    dark = ImageEnhance.Brightness(receipt()).enhance(.2)
    _, processed, info = prepare(dark, lambda image: (0, 0))
    assert ImageStat.Stat(processed).mean[0] > ImageStat.Stat(dark).mean[0] * 2
    _, processed, info = prepare(receipt().resize((300, 400)), lambda image: (0, 0))
    assert info['upscaling'] and processed.size == (900, 1200)


def test_uncertain_rotation_and_no_border():
    _, processed, info = prepare(receipt(), lambda image: (180, 3))
    assert info['rotation'] == 0 and not info['perspective']


def test_perspective():
    image = Image.new('RGB', (1000, 1200), '#333333')
    ImageDraw.Draw(image).polygon([(140, 100), (850, 150), (900, 1080), (100, 1100)], fill='white')
    processed, corrected = perspective(image)
    assert corrected and processed.width < image.width
    blank = Image.new('RGB', (1000, 1200), 'white')
    assert perspective(blank)[1] is False


def test_preprocessing_failure_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr('ocr_preprocessing.ImageOps.autocontrast', Mock(side_effect=RuntimeError('broken')))
    path = tmp_path / 'receipt.png'
    receipt().save(path)
    monkeypatch.setattr('pytesseract.image_to_string', lambda *a, **k: 'Rechnung 119,00 EUR')
    result = LocalDocumentExtractor().extract(path)
    assert '119,00' in result.text
    assert any('Original verwendet' in warning for warning in result.warnings)


def test_alternate_bounded_and_best_selected(monkeypatch):
    monkeypatch.setattr(LocalDocumentExtractor, 'orientation', lambda *args: (0, 0))
    ocr = Mock(side_effect=['?!!', 'Rechnung Netto Brutto MwSt USt EUR 09.09.2026 119,00'])
    monkeypatch.setattr('pytesseract.image_to_string', ocr)
    extractor = LocalDocumentExtractor()
    assert '119,00' in extractor.ocr_image(receipt())
    assert ocr.call_count == 2 and extractor._debug[0]['variant'] == 'alternate'
    assert quality_score('?!'*1000) < quality_score('Rechnung 119,00 EUR')


def test_digital_pdf_exact_text_no_preprocessing(monkeypatch, tmp_path):
    path = tmp_path / 'digital.pdf'
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 40), 'Rechnung Muster GmbH 09.09.2026 Brutto 119,00 EUR')
        expected = document[0].get_text(sort=True)
        document.save(path)
    preprocess = Mock(side_effect=AssertionError('must not preprocess'))
    monkeypatch.setattr('ocr_preprocessing.prepare', preprocess)
    result = LocalDocumentExtractor().extract(path)
    assert result.text == expected and not result.used_ocr
    preprocess.assert_not_called()


def test_exif_rotation():
    image = receipt().rotate(90, expand=True)
    image.getexif()[274] = 6
    original, _, info = prepare(image, lambda image: (0, 0))
    assert original.size == (1200, 1600) and info['exif_rotation'] == 90


def test_good_text_only_one_attempt_and_debug_images(monkeypatch, tmp_path):
    monkeypatch.setattr(LocalDocumentExtractor, 'orientation', lambda *args: (0, 0))
    good = 'Rechnung Netto Brutto MwSt USt EUR 09.09.2026 119,00 Lieferant Muster GmbH ' * 10
    ocr = Mock(return_value=good)
    monkeypatch.setattr('pytesseract.image_to_string', ocr)
    extractor = LocalDocumentExtractor(debug_image_dir=tmp_path)
    assert extractor.ocr_image(receipt()) == good
    assert ocr.call_count == 1
    assert (tmp_path / 'image-001-original.png').is_file()
    assert (tmp_path / 'image-001-processed.png').is_file()
    assert not list(tmp_path.glob('*alternate*'))


def test_alternative_failure_preserves_text(monkeypatch):
    monkeypatch.setattr(LocalDocumentExtractor, 'orientation', lambda *args: (0, 0))
    ocr = Mock(side_effect=['Rechnung', RuntimeError('timeout')])
    monkeypatch.setattr('pytesseract.image_to_string', ocr)
    extractor = LocalDocumentExtractor()
    assert extractor.ocr_image(receipt()) == 'Rechnung'
    assert ocr.call_count == 2
    assert extractor._debug[0]['warnings']


def test_corrupt_image_does_not_crash(tmp_path):
    path = tmp_path / 'broken.jpg'
    path.write_bytes(b'broken')
    result = LocalDocumentExtractor().extract(path)
    assert not result.text and result.warnings


def test_small_tilt():
    from ocr_preprocessing import deskew
    image = receipt().convert('L').rotate(2, fillcolor=255)
    _, angle = deskew(image)
    assert angle == pytest.approx(-2, abs=.5)


@pytest.mark.parametrize('kind', ['dark', 'small'])
def test_real_degraded_receipt(kind):
    image = receipt()
    image = ImageEnhance.Brightness(image).enhance(.2) if kind == 'dark' else image.resize((450, 600))
    text = LocalDocumentExtractor().ocr_image(image)
    assert 'Rechnung' in text and '119,00' in text


def test_debug_voucher_is_local_and_read_only(tmp_path, monkeypatch, capsys):
    from database import Database
    from main import debug_voucher
    from models import AnalysisResult
    monkeypatch.chdir(tmp_path)
    Path('cache').mkdir()
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 40), 'Rechnung Muster GmbH 09.09.2026 Brutto 119,00 EUR')
        document.save('cache/file.pdf')
    database = Database()
    database.save(AnalysisResult(lexware_voucher_id='voucher', file_id='file'))
    database.connection.close()
    before = Path('buchhaltung.db').read_bytes()
    forbidden = Mock(side_effect=AssertionError('No network or AI allowed'))
    monkeypatch.setattr('lexware_api.LexwareClient._request', forbidden)
    monkeypatch.setattr('analyzer.OpenAIAnalyzer.analyze_files', forbidden)
    monkeypatch.setattr('ollama_client.OllamaClient.analyze', forbidden)
    debug_voucher('voucher')
    assert Path('buchhaltung.db').read_bytes() == before
    assert 'direkter Text' in capsys.readouterr().out
    forbidden.assert_not_called()
