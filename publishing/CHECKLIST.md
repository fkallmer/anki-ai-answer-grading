# Veröffentlichungs-Checkliste (AnkiWeb)

## Vorbereitung (einmalig)

- [ ] GitHub-Repo anlegen (empfohlen, als Support-Link):
      Projektordner pushen; `README.md` (deutsch) + `LICENSE` liegen bereits im Root.
      Danach in `publishing/ankiweb_description.html` den Platzhalter
      `GITHUB_URL_HERE` durch die Repo-URL ersetzen.
- [ ] Optional: 1–2 Screenshots machen (Antwortfeld + Feedback-Panel) —
      AnkiWeb-Beschreibungen mit Bildern konvertieren deutlich besser.
      Screenshots müssen extern gehostet werden (z. B. im GitHub-Repo) und
      per <img>-Tag in die Beschreibung.

## Upload

1. Auf https://ankiweb.net einloggen
2. https://ankiweb.net/shared/addons → "Upload" → Typ "Add-on"
3. Datei: `ai_answer_grading.ankiaddon` (Root des Projektordners — die
   Version mit LICENSE im Paket)
4. Titel: **AI Answer Grading — LLM feedback for typed answers**
   (vorher kurz prüfen, dass der Name auf AnkiWeb noch frei ist)
5. Beschreibung: Inhalt von `ankiweb_description.html` einfügen
6. Support-URL: GitHub-Repo (Issues-Seite)

## Nach dem Upload

- [ ] Die zugewiesene Add-on-ID notieren und ins README aufnehmen
      ("Installation via Add-on-ID: XXXXXXXX")
- [ ] Selbst einmal per ID in einer frischen Anki-Installation installieren
      und die Test-Bewertung ausführen
- [ ] Updates: einfach neue .ankiaddon-Datei auf derselben Seite hochladen

## Bekannte Konkurrenz (Stand Juli 2026, zur Abgrenzung in der Beschreibung)

| Addon | Provider | Unterscheidung zu uns |
|---|---|---|
| MyAnswerChecker (1043318428) | OpenAI-kompatibel, Gemini | kein Anthropic/Bedrock, kein PDF-Skript-Kontext, keine Image Occlusion; nutzt u. a. Antwortzeit fürs Rating |
| Anki Type answer Analysis AI (357495808) | ? | fokussiert auf type:answer-Felder |
| Anki Answer Evaluation (1399280435) | ? | einfacher Evaluator |
| Anki Terminator V2 (1468920185) | ChatGPT/DeepSeek | Sidebar-Chat, kein Rating-Durchgriff |

Unsere Alleinstellungsmerkmale (in der Beschreibung betont):
Anthropic + AWS Bedrock, PDF-Vorlesungsskript-Kontext mit Prompt-Caching,
natives Image-Occlusion-Grading, konservatives FSRS-bewusstes Rating,
anpassbarer Prompt bei garantiert stabilem JSON-Format.
