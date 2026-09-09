from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def find_tesseract() -> str | None:
    candidates = [os.getenv("TESSERACT_CMD"), shutil.which("tesseract"),
                  r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                  str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs/Tesseract-OCR/tesseract.exe")]
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


@dataclass
class ExtractedDocument:
    text: str = ""
    used_ocr: bool = False
    warnings: list[str] = field(default_factory=list)
    ocr_debug: list[dict] = field(default_factory=list)
    page_texts: list[str] = field(default_factory=list)


class LocalDocumentExtractor:
    def __init__(self, save_ocr_text: bool = False, debug_image_dir: Path | None = None):
        self.save_ocr_text = save_ocr_text
        self._ocr_language: str | None = None
        self.debug_image_dir = debug_image_dir
        self._debug: list[dict] = []
        self._debug_file = 'image'

    @staticmethod
    def orientation(image):
        import pytesseract
        try:
            data = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT, timeout=20)
            rotation, confidence = int(data['rotate']), float(data['orientation_conf'])
            # Moderate evidence needs an independent upright confirmation.
            if rotation in (90, 180, 270) and 6 <= confidence < 15:
                upright = image.rotate(-rotation, expand=True, fillcolor=255)
                check = pytesseract.image_to_osd(upright, output_type=pytesseract.Output.DICT, timeout=20)
                if int(check['rotate']) == 0 and float(check['orientation_conf']) >= 6:
                    confidence = 15
            return rotation, confidence
        except Exception:
            return 0, 0.0

    @staticmethod
    def usable(text: str) -> bool:
        return len(re.findall(r"[\w]", text)) >= 30

    def ocr_image(self, image) -> str:
        import pytesseract
        command = find_tesseract()
        if not command:
            raise RuntimeError("Tesseract nicht gefunden; lokale OCR nicht verfuegbar")
        pytesseract.pytesseract.tesseract_cmd = command
        if self._ocr_language is None:
            languages = set(pytesseract.get_languages(config="")) - {"osd"}
            if not languages:
                raise RuntimeError("Keine Tesseract-Sprachdaten vorhanden")
            preferred = [lang for lang in ("deu", "eng") if lang in languages]
            self._ocr_language = "+".join(preferred) if preferred else sorted(languages)[0]
        from ocr_preprocessing import prepare, alternate, quality_score
        original, processed, info = prepare(image, self.orientation)
        self._debug.append(info)
        def recognize(candidate):
            return pytesseract.image_to_string(candidate, lang=self._ocr_language, timeout=90)
        try:
            text = recognize(processed)
        except Exception as exc:
            info['warnings'].append(f'Optimierte OCR fehlgeschlagen: {exc}')
            text = recognize(original)
            info['variant'] = 'original'
            info['quality_score'] = quality_score(text)
            return text
        score = quality_score(text)
        alternative = None
        if score < 65:
            try:
                alternative = alternate(processed)
                other = recognize(alternative)
                other_score = quality_score(other)
                if other_score > score:
                    text, score = other, other_score
                    info['variant'] = 'alternate'
            except Exception as exc:
                info['warnings'].append(f'Alternative OCR ausgelassen: {exc}')
        info['quality_score'] = score
        if self.debug_image_dir:
            try:
                self.debug_image_dir.mkdir(parents=True, exist_ok=True)
                prefix = f'{self._debug_file}-{len(self._debug):03d}'
                for name, candidate in [('original', original), ('processed', processed), ('alternate', alternative)]:
                    if candidate is not None:
                        candidate.save(self.debug_image_dir / f'{prefix}-{name}.png')
            except Exception as exc:
                info['warnings'].append(f'Debugbilder nicht gespeichert: {exc}')
        return text

    def extract(self, path: Path, file_id: str | None = None) -> ExtractedDocument:
        result = ExtractedDocument()
        self._debug = []
        self._debug_file = re.sub(r'[^\w-]', '_', file_id or path.stem)
        chunks = []
        try:
            if path.suffix.lower() == ".pdf":
                try:
                    import pymupdf
                except ImportError:
                    from pypdf import PdfReader
                    for index, page in enumerate(PdfReader(str(path)).pages, 1):
                        text = page.extract_text() or ""
                        chunks.append(text)
                        if not self.usable(text):
                            result.warnings.append(f"Seite {index}: PyMuPDF fehlt; OCR-Rendering nicht verfuegbar")
                else:
                    with pymupdf.open(path) as document:
                        for index, page in enumerate(document, 1):
                            text = page.get_text(sort=True)
                            if not self.usable(text):
                                result.used_ocr = True
                                try:
                                    from PIL import Image
                                    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
                                    with Image.frombytes("RGB", (pix.width, pix.height), pix.samples) as image:
                                        text = self.ocr_image(image)
                                except Exception as exc:
                                    result.warnings.append(f"Seite {index}: OCR fehlgeschlagen: {exc}")
                            chunks.append(text)
            elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
                from PIL import Image, ImageOps, ImageSequence
                result.used_ocr = True
                with Image.open(path) as image:
                    for index, frame in enumerate(ImageSequence.Iterator(image), 1):
                        try:
                            chunks.append(self.ocr_image(frame))
                        except Exception as exc:
                            chunks.append("")
                            result.warnings.append(f"Bildseite {index}: OCR fehlgeschlagen: {exc}")
            elif path.suffix.lower() in {".xml", ".txt", ".csv"}:
                text = path.read_text(encoding="utf-8-sig")
                if path.suffix.lower() == ".xml":
                    from xml.etree import ElementTree
                    text = "\n".join(ElementTree.fromstring(text).itertext())
                chunks.append(text)
            else:
                result.warnings.append(f"Datei nicht unterstuetzt: {path.name}")
        except Exception as exc:
            result.warnings.append(f"Lokale Textextraktion fehlgeschlagen ({path.name}): {exc}")
        result.text = "\n".join(chunks)
        result.page_texts = chunks.copy()
        result.ocr_debug = self._debug.copy()
        result.warnings.extend(w for item in self._debug for w in item['warnings'])
        if not result.text.strip():
            result.warnings.append("Kein lesbarer Dokumenttext vorhanden")
        if self.save_ocr_text:
            try:
                target = Path("cache/ocr") / (re.sub(r"[^\w.-]", "_", file_id or path.name) + ".txt")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(result.text, encoding="utf-8")
            except OSError as exc:
                result.warnings.append(f"Debug-Text konnte nicht gespeichert werden: {exc}")
        return result
