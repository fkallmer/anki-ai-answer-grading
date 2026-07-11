# AI Answer Grading (Anki-Addon)

Aktives Abfragen von Karteikarten: Du tippst deine Antwort in ein Textfeld unter
der Karte, ein LLM (Anthropic API **oder** AWS Bedrock) bewertet sie inhaltlich
gegen die Kartenrückseite — optional mit deinem Vorlesungsskript als Kontext —
und leitet konservativ ein Rating (Again/Hard/Good/Easy) ab, das ins echte
Anki-Scheduling (FSRS-kompatibel) übernommen wird.

## Ablauf im Reviewer

1. Auf der Kartenvorderseite erscheint ein mehrzeiliges Textfeld plus Button
   **„Bewerten"** (Shortcut: **Strg+Enter** bzw. **Cmd+Enter**).
2. Nach dem Klick wird die Karte aufgedeckt, die Bewertung läuft asynchron im
   Hintergrund (UI blockiert nie), ein Ladeindikator erscheint.
3. Das Feedback-Panel zeigt Score (0–100), abgeleitetes Rating, Begründung
   (korrekt / fehlt / falsch) und der vorgeschlagene Ease-Button wird farbig
   markiert. Mit `auto_answer: true` wird das Rating nach kurzer Verzögerung
   automatisch gedrückt.
4. Leeres Textfeld + normales Aufdecken → Anki verhält sich wie gewohnt,
   kein API-Call.
5. Bei jedem Fehler (Timeout, HTTP, Rate-Limit, Parse-Fehler) wird die Karte
   normal aufgedeckt, ein Hinweis erscheint im Panel, manuelles Bewerten
   funktioniert uneingeschränkt.

## Installation

**Variante A — Ordner kopieren:**
Kopiere den Ordner `ai_answer_grading/` in deinen Anki-Addon-Ordner
(`Extras → Erweiterungen → Ansicht Dateien…`, das ist `addons21/`), dann Anki
neu starten.

**Variante B — .ankiaddon-Paket:**
Doppelklick auf `ai_answer_grading.ankiaddon` oder in Anki
`Extras → Erweiterungen → Aus Datei installieren…`.

Benötigt aktuelles Anki (Qt6, 23.10+, getestet gegen die 24.x/25.x-API).

## Konfiguration

`Extras → Erweiterungen → AI Answer Grading → Konfiguration`. Alle Optionen
sind in `config.md` dokumentiert (in Anki direkt neben dem Config-Editor
sichtbar). Kurzfassung:

### Provider Anthropic (Default)

```json
{
    "provider": "anthropic",
    "api_key": "sk-ant-…",
    "model": "claude-sonnet-4-6"
}
```

Ohne `api_key` in der Config wird die Umgebungsvariable `ANTHROPIC_API_KEY`
gelesen.

### Provider AWS Bedrock

```json
{
    "provider": "bedrock",
    "aws_region": "eu-central-1",
    "aws_access_key_id": "AKIA…",
    "aws_secret_access_key": "…",
    "bedrock_model": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
}
```

Leere AWS-Felder fallen auf die Umgebungsvariablen `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` zurück. `bedrock_model` ist die
Modell- bzw. Inference-Profile-ID aus deiner AWS-Konsole (Bedrock → Model
access) — welche IDs verfügbar sind, hängt von Konto und Region ab.

### Vorlesungsskripte

```json
"deck_context_map": {
    "Medizin::Anatomie": ["/Users/ich/Skripte/anatomie.pdf"],
    "Jura": ["/Users/ich/Skripte/verwr_1.pdf", "/Users/ich/Skripte/notizen.txt"]
}
```

Deck-Namen matchen per Präfix (Subdecks eingeschlossen, längster Präfix
gewinnt). PDFs werden beim ersten Zugriff extrahiert (vendored `pypdf` in
`lib/`) und als `.txt` im `user_files/`-Ordner gecacht; der Cache wird bei
geänderter mtime/Dateigröße der Quelle invalidiert. Das Skript dient dem
Modell nur zur Einordnung von Synonymen/Notation, nicht als zusätzlicher
Bewertungsmaßstab.

## Tastatur & Komfort

- **Strg+Enter** (macOS: Cmd+Enter) bewertet auf der Vorderseite die getippte
  Antwort und bestätigt auf der Rückseite das vorgeschlagene Rating; die
  Tasten **1–4** überstimmen wie in Anki üblich.
- Unter dem Antwortfeld zeigt eine Zeile **„📄 Skript geladen: …“**, welche
  Dokumente für das aktuelle Deck hinterlegt sind.
- Beim ersten Aufruf einer Karte aus einem Deck mit Skript wird der
  Prompt-Cache des Providers im Hintergrund vorgewärmt, damit schon die erste
  Bewertung der Session schnell läuft (Fehler dabei werden nur geloggt).

## Test ohne Review-Session

`Extras → AI Answer Grading: Test-Bewertung ausführen` schickt einen
Beispiel-Grading-Call mit einer Dummy-Karte an die API und zeigt das Ergebnis
(oder die Fehlermeldung) an — praktisch, um Key und Config zu prüfen.

## Entwicklungs-Tests

```bash
python3 tests/run_tests.py
```

Läuft ohne Anki und ohne API-Key. Abgedeckt: defensives JSON-Parsing
(Fences, Prosa, kaputtes JSON, Clamping), PDF-Extraktion inkl. Cache,
Deck-Präfix-Matching, Verhalten ohne API-Key/AWS-Credentials,
SigV4-Header-Struktur, Prompt-Aufbau (Cache-Breakpoint, konservatives
Rating-Mapping).

## Entscheidungen

- **Bedrock ohne boto3:** Anki-Addons können keine Pakete installieren, und
  boto3 ist zu schwergewichtig zum Vendoren. Bedrock wird daher über die
  stabile `bedrock-runtime`-InvokeModel-HTTP-API angesprochen; die
  AWS-SigV4-Signierung ist in purem Python (stdlib `hmac`/`hashlib`)
  implementiert (`aws_sigv4.py`).
- **Prompt Caching:** Der Skripttext wird als eigener System-Block mit
  `cache_control: {"type": "ephemeral"}` gesendet (Struktur: Regeln → Skript
  (cached) → Karte+Antwort). Unterhalb der modellabhängigen Mindestgröße
  (~2048 Tokens) cached die API stillschweigend nicht — harmlos. Lehnt ein
  Bedrock-Modell `cache_control` ab (HTTP 400), wird einmal ohne Caching
  wiederholt.
- **Private Reviewer-APIs:** Zum Aufdecken (`reviewer._showAnswer()`) und
  automatischen Bewerten (`reviewer._answerCard(ease)`) gibt es keinen
  öffentlichen Weg; beide Aufrufe sind mit `hasattr`-Checks gekapselt und
  degradieren bei API-Änderungen zu einer Log-Warnung statt eines Crashes.
- **`auto_answer` mit Verzögerung:** Sofortiges Drücken würde das Feedback
  unlesbar machen; Default 2,5 s (`auto_answer_delay_ms`), abgebrochen wenn
  du vorher selbst bewertest oder die Karte wechselst.
- **Autofokus im Textfeld:** Für aktives Abfragen liegt der Fokus direkt im
  Textfeld. Solange es fokussiert ist, tippt die Leertaste ein Leerzeichen
  statt aufzudecken — einmal außerhalb klicken stellt das normale
  Anki-Verhalten wieder her.
- **Kartentext:** Es wird der gerenderte, HTML-bereinigte Fragen-/Antworttext
  an das Modell geschickt (`card.answer()` enthält bei Standard-Templates auch
  die Vorderseite — gewollt, das gibt dem Modell Kontext).
- **Konservatives Rating:** Das System-Prompt schreibt das Mapping explizit
  vor und instruiert, im Zweifel das niedrigere Rating zu wählen, damit das
  Spaced-Repetition-Scheduling nicht durch Wohlwollen verfälscht wird.

## Bilder & Image Occlusion

- Bilder auf Karten (JPG/PNG/GIF/WebP) werden mitgeschickt und in die
  Bewertung einbezogen (`send_images`, Default an; max. `max_images`,
  Default 3, je ≤ 4,5 MB). Achtung: erhöht den Token-Verbrauch spürbar.
- **Image Occlusion** (nativer Anki-Notiztyp) wird automatisch erkannt: Das
  Modell bekommt das Originalbild plus die Koordinaten des abgefragten
  Bereichs (rect/ellipse/polygon aus dem Occlusion-Feld) und bewertet, ob
  die Antwort genau diesen Bereich trifft. Da das Modell den Bereich über
  Koordinaten lokalisiert, kann es bei eng beieinanderliegenden
  Beschriftungen ungenau sein — das Feedback-Panel zeigt dann einen
  Unsicherheits-Hinweis. Karten des alten Addons „Image Occlusion Enhanced"
  werden nicht als IO erkannt (anderes Datenformat) und wie normale
  Bildkarten behandelt.

## Bekannte Einschränkungen

- Audio auf Karten sieht das Modell nicht.
- Cloze-Karten funktionieren, aber die Bewertung sieht den gesamten
  gerenderten Text, nicht nur die aktuelle Lücke.
- Pro Karte läuft eine Bewertung; wer während des Ladens zur nächsten Karte
  springt, verwirft das Ergebnis stillschweigend.
- Der API-Key liegt im Klartext in der Addon-Config (Anki bietet keinen
  Secret-Store); alternativ Umgebungsvariablen nutzen.
- Bedrock: keine automatische Credential-Chain wie in boto3 (kein
  `~/.aws/credentials`-Parsing, keine SSO-Profile) — nur Config oder
  Umgebungsvariablen.

## Datenschutz & Kosten

Das Addon sendet Karteninhalte (Frage, Rückseite, deine getippte Antwort,
Kartenbilder und Auszüge hinterlegter Skripte) an den gewählten Provider
(Anthropic bzw. AWS). Gesendet wird nur, wenn du aktiv eine Antwort zur
Bewertung abschickst. Jeder Bewertungs-Call kostet Geld beim Provider;
API-Keys liegen im Klartext in der Anki-Addon-Config.

## Lizenz

GNU AGPLv3 (siehe `LICENSE`) — wie Anki selbst. Enthält pypdf
(BSD-3-Clause) als vendored Dependency.

## Paket bauen

```bash
cd ai_answer_grading
zip -r ../ai_answer_grading.ankiaddon . -x "*__pycache__*" -x "user_files/*" -x "meta.json"
```
