# AI Answer Grading — Konfiguration

## Provider

- **`provider`**: `"anthropic"` (Default) oder `"bedrock"`.

### Anthropic API (`provider: "anthropic"`)

- **`api_key`**: Anthropic API-Key (`sk-ant-...`). Alternativ wird die
  Umgebungsvariable `ANTHROPIC_API_KEY` als Fallback gelesen.
- **`model`**: Modell-ID, Default `"claude-sonnet-4-6"`.

### AWS Bedrock (`provider: "bedrock"`)

- **`aws_region`**: AWS-Region, z. B. `"eu-central-1"` oder `"us-east-1"`.
- **`bedrock_api_key`**: Bedrock-API-Schlüssel (Bearer-Token, beginnt meist mit
  `ABSK…`), erzeugt in der AWS-Konsole unter Bedrock → API-Schlüssel. Der
  einfachste Weg — wenn gesetzt, sind keine weiteren AWS-Credentials nötig.
  Fallback: Umgebungsvariable `AWS_BEARER_TOKEN_BEDROCK`.
- **`aws_access_key_id`** / **`aws_secret_access_key`** / **`aws_session_token`**:
  Alternative zu `bedrock_api_key`: klassische IAM-Credentials (SigV4).
  Wenn leer, werden die Umgebungsvariablen `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` gelesen.
  (`aws_session_token` ist nur für temporäre Credentials nötig.)
- **`bedrock_model`**: Bedrock-Modell-ID bzw. Inference-Profile-ID, z. B.
  `"eu.anthropic.claude-sonnet-4-5-20250929-v1:0"`. Welche IDs verfügbar
  sind, hängt von deinem Bedrock-Konto und der Region ab (AWS-Konsole →
  Bedrock → Model access).

## Verhalten

- **`auto_answer`**: `true` = das vom LLM abgeleitete Rating wird bei
  **Good/Easy automatisch** gedrückt; bei **Again/Hard pausiert** der
  Auto-Modus immer, damit du die Erklärung lesen und ggf. überstimmen kannst.
  `false` (Default) = das Rating wird nur als Vorschlag auf dem Ease-Button
  markiert. Umschaltbar auch per Checkbox in der Abfrage oder im Menü.
- **`auto_answer_delay_ms`**: Wartezeit in Millisekunden, bevor bei
  `auto_answer: true` das Rating gedrückt wird (Default 2500), damit das
  Feedback lesbar bleibt.
- **`feedback_language`**: Sprache des Feedbacks, Default `"Deutsch"`.
- **`custom_prompt`**: Eigene Bewertungsregeln (Persona, Strenge,
  Rating-Kriterien) als Ersatz für den Standard-Prompt; leer = Standard.
  Bequemer editierbar über `Extras → AI Answer Grading → Einstellungen… →
  Tab „Bewertungs-Prompt"`. Der Platzhalter `{language}` wird durch
  `feedback_language` ersetzt. Das JSON-Ausgabeformat wird immer automatisch
  angehängt und ist nicht veränderbar (das Addon muss die Antwort parsen).
- **`request_timeout_s`**: HTTP-Timeout in Sekunden (Default 60).
- **`debug_logging`**: `true` = ausführliches Logging in die Anki-Konsole
  (der API-Key wird niemals geloggt).

## Bilder & Image Occlusion

- **`send_images`**: `true` (Default) = Bilder auf der Karte (JPG/PNG/GIF/WebP)
  werden mit an das Modell geschickt und in die Bewertung einbezogen.
  Achtung: Bilder erhöhen den Token-Verbrauch pro Anfrage deutlich.
- **`max_images`**: maximale Anzahl Bilder pro Anfrage (Default 3). Bilder
  über 4,5 MB werden übersprungen.
- **`show_source_slides`**: `true` (Default) = wenn ein Vorlesungsskript
  hinterlegt ist, nennt das Modell die relevanten Fundstellen und das Panel
  zeigt die betreffenden Folien als Bild (max. 2). Benötigt Qt's
  PDF-Modul (in aktuellen Anki-Versionen enthalten); fehlt es, wird
  stattdessen nur die Folien-Nummer als Text angezeigt.
- **Image Occlusion** (nativer Anki-Notiztyp): wird automatisch erkannt. Das
  Modell erhält das Originalbild plus die Koordinaten des abgefragten
  Bereichs. Hinweis: Bei eng beieinanderliegenden Beschriftungen kann die
  koordinatenbasierte Bewertung ungenau sein — das Panel zeigt dann einen
  entsprechenden Hinweis.

## Vorlesungsskripte

- **`deck_context_map`**: Mapping von Deck-Namen auf eine oder mehrere PDF-
  oder Textdateien (absolute Pfade). Der Deck-Name matcht per Präfix, d. h.
  ein Eintrag für `"Medizin"` gilt auch für `"Medizin::Anatomie::Kapitel 1"`.

  ```json
  "deck_context_map": {
      "Medizin::Anatomie": ["/Users/ich/Skripte/anatomie.pdf"],
      "Jura": ["/Users/ich/Skripte/verwr_1.pdf", "/Users/ich/Skripte/verwr_2.txt"]
  }
  ```

  Ein einzelner String statt einer Liste ist ebenfalls erlaubt.

- **`max_context_chars`**: Obergrenze für den Skripttext in Zeichen
  (Default 150000). Längere Skripte werden hart abgeschnitten; es erscheint
  eine Warnung im Log.
