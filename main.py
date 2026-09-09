from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from analyzer import DocumentAnalyzer
from database import Database, has_openai_error
from exporter import export_results
from lexware_api import LexwareClient, MIME_EXTENSIONS
from models import AnalysisResult
from preflight import detect_non_booking_document, detect_zbon, detect_zbon_text
from validator import validate_analysis


ROOT = Path(__file__).parent


def run_test(client: LexwareClient) -> None:
    profile = client.profile()
    vouchers = client.unchecked_vouchers()
    print(f"Lexware-Verbindung OK: {profile.get('companyName', 'verbunden')}")
    print(f"Unchecked-Belege: {len(vouchers)}")


def vouchers_to_process(vouchers: list[dict], database: Database, limit: int | None, force: bool) -> tuple[list[dict], int, int]:
    known_ids = database.voucher_ids()
    known_count = sum(voucher["id"] in known_ids for voucher in vouchers)
    candidates = vouchers if force else [voucher for voucher in vouchers if voucher["id"] not in known_ids]
    new_count = len(candidates)
    if limit is not None:
        candidates = candidates[:limit]
    return candidates, known_count, new_count


def scan(client: LexwareClient, database: Database, limit: int | None, force: bool, analyzer: DocumentAnalyzer) -> None:
    vouchers = client.unchecked_vouchers()
    vouchers, known_count, new_count = vouchers_to_process(vouchers, database, None, force)
    print(f"Bereits analysiert: {known_count}")
    print(f"Neue Belege verfuegbar: {new_count}")
    analyzed_count = 0
    ignored_count = 0
    reviewed_count = 0
    for index, meta in enumerate(vouchers, 1):
        if reviewed_count >= 100:
            break
        if limit is not None and analyzed_count >= limit:
            break
        reviewed_count += 1
        voucher_id = meta["id"]
        voucher = client.voucher(voucher_id)
        file_ids = voucher.get("files", [])
        paths = []
        downloaded_ids = []
        download_warnings = []
        previous = database.get(voucher_id)
        cached = cached_retry_files(previous, file_ids, Path("cache")) if previous else [None] * len(file_ids)
        for file_id, cached_path in zip(file_ids, cached):
            try:
                path = cached_path if cached_path is not None else client.download_file(file_id, Path("cache") / file_id)
            except Exception as exc:
                print(f"{index}: PRUEFEN {voucher_id} - {exc}")
                download_warnings.append(f"Belegdatei {file_id} fehlt: {exc}")
                continue
            paths.append(path)
            downloaded_ids.append(file_id)

        zbon, zbon_warning = detect_zbon_text(json.dumps(voucher, ensure_ascii=False))
        if not zbon:
            zbon, zbon_warning = detect_zbon(paths)
        if zbon:
            database.save_ignored(voucher_id, downloaded_ids or file_ids, "zbon", "IGNORIEREN_ZBON", zbon_warning or "Z-Bon erkannt")
            print(f"{index}: IGNORIERT - Z-BON {voucher_id} - {zbon_warning}")
            ignored_count += 1
            continue

        non_booking, document_type, non_booking_warning = detect_non_booking_document(paths)
        if non_booking:
            database.save_ignored(voucher_id, downloaded_ids or file_ids, document_type or "sonstiges", "IGNORIEREN_NICHT_BUCHEN", non_booking_warning or "Nicht buchbarer Beleg erkannt")
            print(f"{index}: IGNORIEREN / NICHT BUCHEN {voucher_id} - {non_booking_warning}")
            ignored_count += 1
            continue

        if not file_ids:
            result = analyzer.analyze(Path("kein-dokument"), voucher_id)
            database.save(result)
            print(f"{index}: {result.status} {voucher_id} - {'; '.join(result.warnings)}")
            analyzed_count += 1
            continue
        result = analyzer.analyze_files(paths, voucher_id, downloaded_ids)
        if download_warnings:
            result.warnings.extend(download_warnings)
            result = validate_analysis(result, voucher_id, ",".join(file_ids))
            if not result.status.startswith("IGNORIEREN"):
                result.status = "UNVOLLSTAENDIG"
                result.confidence = min(result.confidence or 0, 0.5)
        database.save(result)
        warning_text = f" - {'; '.join(result.warnings)}" if result.warnings else ""
        print(f"{index}: {result.status} {voucher_id}{warning_text}")
        if result.status in {"IGNORIEREN_ZBON", "IGNORIEREN_KONTOAUSZUG", "IGNORIEREN_KARTENBELEG", "IGNORIEREN_NICHT_BUCHEN"}:
            ignored_count += 1
        else:
            analyzed_count += 1
    print(f"Durchgesehen: {reviewed_count}")
    print(f"Ignoriert: {ignored_count}")
    print(f"Lokal analysiert: {analyzed_count}")
    print(f"Lokale KI verwendet: {getattr(analyzer, 'ollama_calls', 0)}")
    print(f"OpenAI verwendet: {getattr(analyzer, 'openai_calls', 0)}")
    export_results(database.all())


def cached_retry_files(result: AnalysisResult, file_ids: list[str], cache: Path) -> list[Path | None]:
    # Older downloads use Content-Disposition names, retained in fallback hints.
    names = []
    for hint in result.hints:
        prefix, suffix = "Dokument ", " konnte ohne KI nicht analysiert werden"
        if hint.startswith(prefix) and hint.endswith(suffix):
            names = hint[len(prefix):-len(suffix)].split(", ")
            break
        if hint.startswith("Quelldateien: "):
            names = hint[len("Quelldateien: "):].split(", ")
    paths = []
    for index, file_id in enumerate(file_ids):
        candidates = [cache / f"{file_id}{extension}" for extension in sorted(set(MIME_EXTENSIONS.values()))]
        if len(names) == len(file_ids) and Path(names[index]).name == names[index]:
            candidates.insert(0, cache / names[index])
        paths.append(next((path for path in candidates if path.is_file()), None))
    return paths


def retry_openai(client: LexwareClient, database: Database, analyzer: DocumentAnalyzer, *, local: bool = False) -> None:
    candidates = database.openai_failures()
    print(f"{'Lokal erneut zu analysieren' if local else 'Erneut zu analysieren'}: {len(candidates)}")
    succeeded = 0
    failed = 0
    for index, previous in enumerate(candidates, 1):
        voucher_id = previous.lexware_voucher_id
        try:
            file_ids = [value.strip() for value in (previous.file_id or "").split(",") if value.strip()]
            if not file_ids:
                file_ids = client.voucher(voucher_id).get("files", [])
            if not file_ids:
                raise ValueError("Keine Belegdatei vorhanden")
            cached = cached_retry_files(previous, file_ids, Path("cache"))
            paths = [path if path is not None else client.download_file(file_id, Path("cache") / file_id)
                     for file_id, path in zip(file_ids, cached)]
            zbon, warning = detect_zbon(paths)
            non_booking, document_type, non_booking_warning = detect_non_booking_document(paths) if not zbon else (False, None, None)
            if zbon or non_booking:
                result = AnalysisResult(
                    lexware_voucher_id=voucher_id, file_id=",".join(file_ids),
                    document_type="zbon" if zbon else document_type,
                    status="IGNORIEREN_ZBON" if zbon else "IGNORIEREN_NICHT_BUCHEN",
                    warnings=[warning or non_booking_warning or "Nicht buchbarer Beleg erkannt"],
                    analysis_method="rules", confidence=1.0,
                )
            else:
                result = analyzer.analyze_files(paths, voucher_id, file_ids)
            if has_openai_error(result) or any("OPENAI_API_KEY fehlt" in warning for warning in result.warnings):
                failed += 1
            else:
                database.save(result)
                if local and result.status in {"PRUEFEN", "UNVOLLSTAENDIG", "SONDERFALL"}:
                    failed += 1
                else:
                    succeeded += 1
            warning_text = f" - {'; '.join(result.warnings)}" if result.warnings else ""
            print(f"{index}: {result.status} {voucher_id}{warning_text}")
        except Exception as exc:
            failed += 1
            print(f"{index}: UNVOLLSTAENDIG {voucher_id} - {exc}")
    print(f"{'Lokal erfolgreich analysiert' if local else 'Erfolgreich neu analysiert'}: {succeeded}")
    print(f"{'Weiterhin zu prüfen' if local else 'Weiterhin fehlerhaft'}: {failed}")
    if local:
        export_results(database.all())


def retry_local(client: LexwareClient, database: Database, analyzer: DocumentAnalyzer | None = None) -> None:
    analyzer = analyzer or DocumentAnalyzer(local_only=True)
    analyzer.config = replace(analyzer.config, use_openai_fallback=False)
    retry_openai(client, database, analyzer, local=True)


def debug_voucher(voucher_id: str, save_images: bool = False) -> None:
    """Inspect one existing voucher locally; no API, AI or database writes."""
    import sqlite3
    from local_extractor import LocalDocumentExtractor
    from local_rules import extract_rules, debug_candidates
    from local_analyzer import finalize
    with sqlite3.connect('file:buchhaltung.db?mode=ro', uri=True) as connection:
        row = connection.execute('SELECT analyse_json FROM analyses WHERE lexware_voucher_id = ?',
                                 (voucher_id,)).fetchone()
    if row is None:
        raise SystemExit('Voucher nicht in lokaler Datenbank vorhanden')
    previous = AnalysisResult.model_validate_json(row[0])
    ids = [value.strip() for value in (previous.file_id or '').split(',') if value.strip()]
    paths = cached_retry_files(previous, ids, Path('cache'))
    if not paths or any(path is None for path in paths):
        raise SystemExit('Belegdateien fehlen im lokalen Cache')
    safe_id = re.sub(r'[^\w-]', '_', voucher_id)
    extractor = LocalDocumentExtractor(debug_image_dir=Path('debug/images') / safe_id if save_images else None)
    documents = [extractor.extract(path, file_id) for path, file_id in zip(paths, ids)]
    raw_sections = []
    for path, doc in zip(paths, documents):
        raw_sections.append(f'===== DATEI {path.name} =====\n')
        for index, text in enumerate(doc.page_texts, 1):
            raw_sections.append(f'===== OCR ROHTEXT SEITE {index} =====\n{text}\n')
    raw_text = '\n'.join(raw_sections)
    target = Path('debug/text') / f'{safe_id}.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(raw_text, encoding='utf-8')
    print(raw_text)
    print(f'Vollstaendiger Rohtext: {target.resolve()}')
    for path, doc in zip(paths, documents):
        print(f'Datei: {path.name}; Extraktion: {"OCR" if doc.used_ocr else "direkter Text"}')
        for index, info in enumerate(doc.ocr_debug, 1):
            print(f'Seite {index}: Originalgroesse={info["original_size"]}; '
                  f'Rotation={info["rotation"]} Grad; EXIF-Rotation={info["exif_rotation"]} Grad; '
                  f'Perspektivkorrektur={info["perspective"]}; Upscaling={info["upscaling"]}; '
                  f'OCR-Variante={info["variant"]}; OCR-Qualitaetsscore={info["quality_score"]}')
        for warning in doc.warnings:
            print(f'Warnung: {warning}')
    text = '\n\n'.join(doc.text for doc in documents)
    print('===== REGELKANDIDATEN (Fundstellen, keine automatisch bestaetigten Werte) =====')
    print(json.dumps(debug_candidates(text), ensure_ascii=False, indent=2))
    result = finalize(extract_rules(text), voucher_id, ids)
    result.analysis_method = 'ocr_rules' if any(doc.used_ocr for doc in documents) else 'rules'
    print(f'Vorher: {previous.status}; Jetzt: {result.status}')
    print(result.model_dump_json(indent=2))
    print('OpenAI verwendet: 0; Ollama verwendet: 0; Lexware-Aufrufe: 0')


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Lokale Lexware-Beleganalyse (nur GET gegen Lexware)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("test")
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--limit", type=int)
    scan_parser.add_argument("--all", action="store_true")
    scan_parser.add_argument("--force", action="store_true")
    sub.add_parser("export")
    sub.add_parser("retry-openai")
    sub.add_parser("retry-local")
    debug_parser = sub.add_parser('debug-voucher')
    debug_parser.add_argument('voucher_id')
    debug_parser.add_argument('--save-images', action='store_true')
    args = parser.parse_args()
    if args.command == 'debug-voucher':
        debug_voucher(args.voucher_id, args.save_images)
        return
    if args.command == "export":
        export_results(Database().all())
        return
    key = os.getenv("LEXWARE_API_KEY", "").strip()
    if not key:
        raise SystemExit("LEXWARE_API_KEY fehlt: bitte in .env eintragen.")
    client = LexwareClient(key)
    if args.command == "test":
        run_test(client)
    elif args.command == "retry-openai":
        retry_openai(client, Database(), DocumentAnalyzer(os.getenv("OPENAI_API_KEY")))
    elif args.command == "retry-local":
        retry_local(client, Database())
    else:
        scan(client, Database(), None if args.all else args.limit, args.force, DocumentAnalyzer(os.getenv("OPENAI_API_KEY")))


if __name__ == "__main__":
    main()
