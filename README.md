# Lexware Beleganalyse Beta – Local First

Lokale Beta zur Analyse vorhandener Lexware-Office-Belege. Gegen `api.lexware.io` werden ausschliesslich GET-Requests ausgefuehrt. Es gibt keine Buchungs-, Aenderungs-, Upload- oder Loeschfunktion. GRUEN ist nur ein Plausibilitaetsvorschlag und keine automatische Buchung.

## Einrichtung

1. Abhaengigkeiten installieren: `py -m pip install -r requirements.txt`
2. In `.env` den `LEXWARE_API_KEY` eintragen. Ein OpenAI-Key ist fuer den normalen Betrieb nicht erforderlich.
3. Fuer Bildbelege und gescannte PDF-Seiten Tesseract separat installieren (siehe unten). Digitale PDFs funktionieren ohne Tesseract.

Standardkonfiguration (auch ohne diese Eintraege ist OpenAI deaktiviert):

```dotenv
USE_OPENAI_FALLBACK=false
USE_OLLAMA=false
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=
SAVE_OCR_TEXT=false
```

Der Ablauf bleibt automatisch: Lexware GET → Datei/Cache → lokale Klassifikation → PDF-Text oder OCR → Python-Regeln → SQLite → CSV/HTML. Es sind keine manuellen Downloads notwendig. Bei fehlenden OCR-Komponenten wird ein pruefbares, unvollstaendiges Ergebnis gespeichert; es erfolgt kein automatischer Wechsel zu einer Cloud-API.

## Lokale OCR und optionale KI

Tesseract ist ein separates Programm; `pytesseract` allein installiert es nicht. Unter Windows einen Tesseract-Installer mit den Sprachdaten `deu` und `eng` installieren. Bezugsquelle und Windows-Hinweise: [Tesseract-Dokumentation](https://tesseract-ocr.github.io/tessdoc/Installation.html). Automatische Suche: `TESSERACT_CMD`, PATH, `C:\Program Files\Tesseract-OCR\tesseract.exe`, x86- und Benutzer-Installationspfad. Ein abweichender Pfad kann ueber `TESSERACT_CMD` gesetzt werden.

Digitale PDF-Seiten werden mit PyMuPDF gelesen; Seiten ohne brauchbaren Text werden einzeln mit 300 dpi gerendert und lokal per Tesseract gelesen. Alle PDF-Seiten und TIFF-Bilder werden beruecksichtigt. Sprachen: `deu+eng`, soweit vorhanden, sonst eine installierte Sprache. XML/TXT/CSV werden lokal gelesen. Mit `SAVE_OCR_TEXT=true` werden extrahierte Texte unter `cache/ocr/<file_id>.txt` gespeichert; diese Dateien enthalten sensible Belegdaten.

Ollama ist optional und wird nicht automatisch installiert. Installation: [Ollama fuer Windows](https://docs.ollama.com/windows). Ein lokal installiertes Modell in `OLLAMA_MODEL` eintragen und `USE_OLLAMA=true` setzen. Nur lokale HTTP-Endpunkte (`localhost`, `127.0.0.1`, `::1`) sind erlaubt; Cloud-Modellnamen und HTTP-Weiterleitungen werden abgelehnt. Ollama wird nur bei unzureichenden Regeln aufgerufen. Bei einem Ausfall bleibt das Regelergebnis mit der Warnung „Lokale KI nicht verfügbar“ erhalten. Modell-Ergaenzungen werden als Vorschlaege zur manuellen Pruefung markiert, Werte ohne Beleg im Text verworfen.

OpenAI wird erst mit `USE_OPENAI_FALLBACK=true` und unzureichendem lokalem Ergebnis verwendet. **Nur dann koennen Dateien an OpenAI gesendet werden und API-Kosten entstehen.** Bei false wird kein OpenAI-Client initialisiert. Dauerhafte Key-/Guthabenfehler sperren weitere OpenAI-Aufrufe fuer den aktuellen Lauf; SDK-Wiederholungen sind deaktiviert.

## Befehle

`py main.py test` testet die Verbindung ohne KI-Kosten.

`py main.py scan --limit 3` analysiert maximal drei bisher nicht gespeicherte unchecked-Belege.

`py main.py scan --all` analysiert alle noch nicht gespeicherten unchecked-Belege.

`py main.py scan --limit 3 --force` analysiert drei Belege erneut.

`py main.py retry-local` verarbeitet ausschliesslich gespeicherte `UNVOLLSTAENDIG`-Analysen mit OpenAI-Fehlerwarnungen (401, invalid_api_key, credit_balance_exhausted, insufficient_quota oder „OpenAI Analyse fehlgeschlagen“). Erkannte Z-Bons bleiben ausgeschlossen. Cache-Dateien werden wiederverwendet, fehlende Dateien ausschliesslich per GET geladen. Dieser Befehl sperrt OpenAI auch dann, wenn der Fallback in `.env` aktiviert ist. Optionale lokale KI bleibt moeglich.

Auch ein lokal unvollstaendiges Ergebnis ersetzt bei `retry-local` die alte API-Fehlermeldung in SQLite und zaehlt als „Weiterhin zu prüfen“. Nur GRUEN oder lokal als ignorierbar erkannte Dokumente zaehlen als erfolgreich. Bei einem Downloadfehler bleibt der alte Datensatz erhalten. CSV/HTML werden anschliessend aktualisiert. Bereits lokal verarbeitete Belege werden nicht nochmals von `retry-local` ausgewaehlt.

`py main.py retry-openai` bleibt als kompatibler Befehl mit derselben gezielten Fehlerauswahl erhalten. Auch hier laeuft zuerst die lokale Pipeline und OpenAI nur bei ausdruecklich aktiviertem Fallback. Exporte bei diesem Befehl anschliessend mit `py main.py export` aktualisieren.

`py main.py export` erzeugt `output/vorschlaege.csv` und `output/pruefung.html` neu.

Die Ergebnisse liegen lokal in `buchhaltung.db`; heruntergeladene Dateien liegen lokal in `cache/`.

## Regeln und Pruefung

Extrahiert werden eindeutig beschriftete Rechnungsfelder sowie Steuergruppen mit 7 % und 19 %. Tabellen brauchen einen erkennbaren Spaltenkopf (Netto, Steuer/MwSt/USt, Brutto). Deutsche Betraege wie `1.234,56` werden beruecksichtigt. Widerspruechliche Werte bleiben null; mathematische Abweichungen fuehren zu PRUEFEN. Kleine Rundungsdifferenzen werden toleriert. Pfand, Leergut, Palettenmiete und weitere Sonderpositionen werden als Hinweise gespeichert, ohne eine steuerliche Behandlung oder Buchung zu erzeugen.

Lieferanten werden aus einer ausdruecklichen Rechnungssteller-Zeile oder aus lokalen Identitaetsregeln erkannt. Eine beliebige Firmenanschrift kann auch die des Empfaengers sein und wird deshalb nicht automatisch als Lieferant uebernommen. Optional `supplier_rules.example.json` nach `supplier_rules.json` kopieren und reale Firmen mit eindeutigen Markern (etwa Firmenname plus USt-ID, IBAN oder Anschrift) eintragen. Alle Marker einer Regel muessen vorkommen; widerspruechliche Zuordnungen bleiben unklar.

Plattformen verwenden weiterhin das bestehende Modell: `restaurant_7_gross`/`restaurant_19_gross` sind Bruttoumsaetze, `commission_tax` die Provisions-USt, `fees_net`/`fees_tax` die sonstigen Gebuehren und `difference` die Auszahlungsdifferenz. Hinzu kommen `settlement_period_from`/`settlement_period_to`. Die Kontrollrechnung beruecksichtigt Trinkgeld, Provision, Gebuehren, Gutschriften, Korrekturen und Erstattungen. Bei lokaler Analyse muessen auch optionale Summen explizit angegeben sein (ggf. 0,00); unbekannte Betraege werden nicht als Null konstruiert. Andernfalls bleibt die erwartete Auszahlung null und eine Warnung fordert zur Pruefung auf.

`analysis_method` zeigt `rules`, `ocr_rules` oder eine Kombination mit `ollama`/`openai`. `confidence` ist ein konservativer Vollstaendigkeits-/Plausibilitaetsindikator von 0 bis 1, keine statistisch kalibrierte Erkennungswahrscheinlichkeit. KI-Ergaenzungen bleiben manuell zu pruefen. Bestehende SQLite-Daten bleiben lesbar; neue Felder werden im vorhandenen JSON gespeichert. HTML zeigt die wichtigsten Rechnungs- und Plattformfelder direkt, alle Details bleiben aufklappbar.

## Tests und erster lokaler Lauf

```powershell
py -m pytest -q -p no:cacheprovider --basetemp=.test-local-run
py main.py retry-local
py main.py scan --limit 10
```

Das pytest-Temporärverzeichnis muss fuer jeden parallelen Testlauf eindeutig sein. Der erste echte Scan sollte bei maximal zehn relevanten Belegen bleiben; ignorierte Z-Bons verbrauchen das Limit nicht.

Technische Referenzen: [PyMuPDF PDF-/Bildextraktion](https://pymupdf.readthedocs.io/en/latest/recipes-images.html), [pytesseract](https://pypi.org/project/pytesseract/), [Ollama JSON-Schema-Ausgaben](https://docs.ollama.com/capabilities/structured-outputs).
# Lokale OCR fuer fotografierte Belege

`debug-voucher` gibt den vollstaendigen extrahierten Text mit Seitenmarkierungen
aus und speichert ihn als UTF-8 unter `debug/text/<voucher-id>.txt`.
Die Regelkandidaten zeigen Rechnungsnummern samt Marker, Zeile und Annahme-/
Ablehnungsgrund sowie Datum, Geldbetraege und Prozentsaetze mit Zeilenkontext.
Lieferantenkandidaten stammen ausschliesslich aus den vorhandenen expliziten
Lieferantenbezeichnungen oder lokalen Identitaetsregeln. Kandidaten sind keine
bestaetigten Analysewerte. Die Zeilenangaben beziehen sich auf den zusammengefuegten
Rohtext ohne die Debug-Seitenueberschriften.
Rechnungsnummern benoetigen einen expliziten Nummernmarker und einen direkt
folgenden Wert mit mindestens einer Ziffer; Feldnamen, Datums-/Betragswerte und
widerspruechliche Nummern werden nicht uebernommen.

Digitale PDF-Seiten mit brauchbarem Text werden weiterhin direkt extrahiert.
Nur Scan-Seiten und JPG/PNG/TIFF durchlaufen die lokale Pillow-Vorverarbeitung:
EXIF, konservative Tesseract-Orientierung, dokumentrandbasierte Perspektivkorrektur,
leichte Schraeglagenkorrektur, Graustufen, Kontrast, Rauschfilter und Schaerfung.
Kleine Bilder werden maximal dreifach vergroessert; grosse OCR-Arbeitsbilder auf
4000 Pixel Kantenlaenge begrenzt. Uneindeutige Dokumentraender bleiben unveraendert.
Die Randerkennung ist bewusst auf helle Dokumente vor dunklerem Hintergrund begrenzt.

Pro Seite erfolgen maximal zwei Texterkennungen. Unter einem heuristischen Score
von 65 wird eine lokale Binarisierung getestet; nur ein hoeherer Score ersetzt
den ersten Text. Der Score (0–100) bewertet Textmenge, Zeichen, Woerter,
Datums-/Betragsmuster und Belegbegriffe; er ist keine Zuverlaessigkeitsgarantie.
OSD ist optional: hohe Konfidenz ab 15, mittlere ab 6 nur mit bestaetigter
aufrechter Orientierung. Bei fehlendem OSD wird nicht geraten.
Tesseract verwendet bevorzugt `deu+eng`, sofern beide Sprachpakete installiert sind.

Einen vorhandenen Voucher ausschliesslich aus lokaler Datenbank und Cache pruefen:

```powershell
python main.py debug-voucher <voucher-id> --save-images
```

Ausgabe: Originalgroesse, EXIF-/OSD-Rotation, Perspektivkorrektur, Upscaling,
gewaehlte OCR-Variante und Qualitaetsscore pro Seite. Optionale Bilder liegen in
`debug/images/<voucher-id>/` mit Datei-/Seitennummer und `original`, `processed`,
gegebenenfalls `alternate`. `original` zeigt das Bild nach EXIF-Ausrichtung.
Der Befehl verwendet weder OpenAI noch Ollama oder Lexware-Aufrufe und speichert
keine Analyse in der Datenbank. Steuer-/Lieferantenregeln bleiben unveraendert.
