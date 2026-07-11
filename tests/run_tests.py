"""Standalone tests for AI Answer Grading (no Anki required).

Run with:  python3 tests/run_tests.py
Covers: defensive JSON parsing, PDF extraction + caching, deck prefix
matching, behavior without API key, SigV4 header structure.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_answer_grading import aws_sigv4, context_store, grader, media  # noqa: E402
from ai_answer_grading.grader import GradingError, parse_grading_json  # noqa: E402


def make_minimal_pdf(text: str) -> bytes:
    """Hand-craft a valid single-page PDF containing `text`."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,  # content stream, built below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


class TestJsonParsing(unittest.TestCase):
    def test_clean_json(self):
        r = parse_grading_json(
            '{"score": 85, "rating": 3, "correct_points": ["a"], '
            '"missing_points": [], "wrong_points": [], "feedback": "Gut."}'
        )
        self.assertEqual(r.score, 85)
        self.assertEqual(r.rating, 3)
        self.assertEqual(r.rating_label, "Good")
        self.assertEqual(r.correct_points, ["a"])
        self.assertEqual(r.feedback, "Gut.")

    def test_markdown_fences(self):
        r = parse_grading_json('```json\n{"score": 40, "rating": 2, "feedback": "ok"}\n```')
        self.assertEqual(r.score, 40)
        self.assertEqual(r.rating, 2)

    def test_prose_around_json(self):
        r = parse_grading_json(
            'Hier ist meine Bewertung:\n{"score": 10, "rating": 1, "feedback": "nein"}\nFertig!'
        )
        self.assertEqual(r.rating, 1)

    def test_score_clamping_and_string_numbers(self):
        r = parse_grading_json('{"score": "150", "rating": "4", "feedback": "x"}')
        self.assertEqual(r.score, 100)
        self.assertEqual(r.rating, 4)
        r = parse_grading_json('{"score": -5, "rating": 1, "feedback": "x"}')
        self.assertEqual(r.score, 0)

    def test_explanation_field(self):
        r = parse_grading_json(
            '{"score": 20, "rating": 1, "explanation": "Der Regelkreis besteht aus …", '
            '"feedback": "Nochmal wiederholen."}'
        )
        self.assertEqual(r.explanation, "Der Regelkreis besteht aus …")
        # explanation is optional — older/other outputs must still parse
        r2 = parse_grading_json('{"score": 90, "rating": 4, "feedback": "Top."}')
        self.assertEqual(r2.explanation, "")

    def test_source_pages_parsing(self):
        r = parse_grading_json(
            '{"score": 50, "rating": 2, "feedback": "x", '
            '"source_pages": ["anatomie.pdf:12", 7, "Seite 3", "skript.pdf S. 4", "quatsch"]}'
        )
        self.assertEqual(
            r.source_pages,
            [("anatomie.pdf", 12), ("", 7), ("", 3), ("skript.pdf", 4)],
        )

    def test_source_pages_absent_or_invalid(self):
        r = parse_grading_json('{"score": 50, "rating": 2, "feedback": "x"}')
        self.assertEqual(r.source_pages, [])
        r2 = parse_grading_json(
            '{"score": 50, "rating": 2, "feedback": "x", "source_pages": {"a": 1}}'
        )
        self.assertEqual(r2.source_pages, [])

    def test_string_instead_of_list(self):
        r = parse_grading_json(
            '{"score": 50, "rating": 2, "missing_points": "die Definition", "feedback": "x"}'
        )
        self.assertEqual(r.missing_points, ["die Definition"])

    def test_invalid_rating(self):
        with self.assertRaises(GradingError):
            parse_grading_json('{"score": 50, "rating": 7, "feedback": "x"}')

    def test_broken_json(self):
        with self.assertRaises(GradingError):
            parse_grading_json('{"score": 50, "rating": ')

    def test_missing_fields(self):
        with self.assertRaises(GradingError):
            parse_grading_json('{"feedback": "nur text"}')

    def test_empty_and_no_json(self):
        with self.assertRaises(GradingError):
            parse_grading_json("")
        with self.assertRaises(GradingError):
            parse_grading_json("Die Antwort war leider falsch.")


class TestNoApiKey(unittest.TestCase):
    def test_missing_anthropic_key_raises_before_network(self):
        env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            config = {"provider": "anthropic", "api_key": ""}
            with self.assertRaises(GradingError) as ctx:
                grader.grade_answer(config, "F", "B", "A", None)
            self.assertIn("API-Key", str(ctx.exception))
        finally:
            if env_backup is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_backup

    def test_bedrock_bearer_key_resolution(self):
        backup = os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
        try:
            self.assertEqual(
                grader._resolve_bedrock_bearer({"bedrock_api_key": " ABSKtest "}), "ABSKtest"
            )
            self.assertEqual(grader._resolve_bedrock_bearer({}), "")
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "ABSKenv"
            self.assertEqual(grader._resolve_bedrock_bearer({}), "ABSKenv")
        finally:
            os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
            if backup is not None:
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = backup

    def test_missing_aws_credentials_raises(self):
        backups = {
            k: os.environ.pop(k, None)
            for k in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_BEARER_TOKEN_BEDROCK",
            )
        }
        try:
            config = {
                "provider": "bedrock",
                "aws_region": "eu-central-1",
                "bedrock_model": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            }
            with self.assertRaises(GradingError) as ctx:
                grader.grade_answer(config, "F", "B", "A", None)
            self.assertIn("AWS-Credentials", str(ctx.exception))
        finally:
            for k, v in backups.items():
                if v is not None:
                    os.environ[k] = v


class TestWarmCache(unittest.TestCase):
    def test_warm_cache_without_key_raises(self):
        backup = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(GradingError):
                grader.warm_cache({"provider": "anthropic", "api_key": ""}, "Skripttext")
        finally:
            if backup is not None:
                os.environ["ANTHROPIC_API_KEY"] = backup

    def test_validate_credentials_returns_provider(self):
        self.assertEqual(
            grader._validate_credentials({"provider": "anthropic", "api_key": "sk-x"}),
            "anthropic",
        )
        with self.assertRaises(GradingError):
            grader._validate_credentials({"provider": "mistral"})

    def test_validate_openai_provider(self):
        # base_url + model required; key optional (local servers)
        self.assertEqual(
            grader._validate_credentials({
                "provider": "openai",
                "openai_base_url": "http://localhost:11434/v1",
                "openai_model": "qwen2.5:32b",
            }),
            "openai",
        )
        with self.assertRaises(GradingError):
            grader._validate_credentials({"provider": "openai", "openai_model": "x"})
        with self.assertRaises(GradingError):
            grader._validate_credentials(
                {"provider": "openai", "openai_base_url": "http://x/v1"}
            )

    def test_extract_text_openai(self):
        self.assertEqual(
            grader._extract_text_openai(
                {"choices": [{"message": {"content": '{"score": 1}'}}]}
            ),
            '{"score": 1}',
        )
        self.assertEqual(
            grader._extract_text_openai(
                {"choices": [{"message": {"content": [
                    {"type": "text", "text": "a"}, {"type": "text", "text": "b"}
                ]}}]}
            ),
            "ab",
        )
        with self.assertRaises(GradingError):
            grader._extract_text_openai({"error": {"message": "nope"}})


class TestDeckMapping(unittest.TestCase):
    MAP = {
        "Medizin": ["/tmp/med.pdf"],
        "Medizin::Anatomie": ["/tmp/anat.pdf"],
        "Jura": "/tmp/jura.pdf",
    }

    def test_exact_match(self):
        self.assertEqual(context_store.resolve_deck_files("Jura", self.MAP), ["/tmp/jura.pdf"])

    def test_subdeck_prefix(self):
        self.assertEqual(
            context_store.resolve_deck_files("Jura::VerwR::Kap1", self.MAP), ["/tmp/jura.pdf"]
        )

    def test_longest_prefix_wins(self):
        self.assertEqual(
            context_store.resolve_deck_files("Medizin::Anatomie::Knochen", self.MAP),
            ["/tmp/anat.pdf"],
        )

    def test_no_partial_word_match(self):
        self.assertEqual(context_store.resolve_deck_files("Juraforum", self.MAP), [])

    def test_no_mapping(self):
        self.assertEqual(context_store.resolve_deck_files("Sonstiges", self.MAP), [])


class TestPdfExtraction(unittest.TestCase):
    def test_extract_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "skript.pdf")
            cache_dir = os.path.join(tmp, "cache")
            with open(pdf_path, "wb") as f:
                f.write(make_minimal_pdf("Anki Skript Testinhalt"))

            ctx = context_store.get_context_for_deck(
                "Deck", {"Deck": [pdf_path]}, cache_dir, max_chars=150000
            )
            self.assertIsNotNone(ctx)
            self.assertIn("Anki Skript Testinhalt", ctx)
            self.assertIn("[Seite 1 von skript.pdf]", ctx)  # page marker for citations

            # A cache .txt must now exist and be reused.
            cached = [f for f in os.listdir(cache_dir) if f.endswith(".txt")]
            self.assertEqual(len(cached), 1)
            ctx2 = context_store.get_context_for_deck(
                "Deck", {"Deck": [pdf_path]}, cache_dir, max_chars=150000
            )
            self.assertEqual(ctx, ctx2)

    def test_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt_path = os.path.join(tmp, "skript.txt")
            with open(txt_path, "w") as f:
                f.write("x" * 5000)
            ctx = context_store.get_context_for_deck(
                "Deck", {"Deck": txt_path}, tmp, max_chars=100
            )
            self.assertEqual(len(ctx), 100)

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context_store.get_context_for_deck(
                "Deck", {"Deck": ["/nope/nirgends.pdf"]}, tmp
            )
            self.assertIsNone(ctx)

    def test_unmapped_deck_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(context_store.get_context_for_deck("Deck", {}, tmp))


class TestSigV4(unittest.TestCase):
    def test_header_structure(self):
        headers = aws_sigv4.sign_request(
            method="POST",
            url="https://bedrock-runtime.eu-central-1.amazonaws.com/model/foo/invoke",
            region="eu-central-1",
            service="bedrock",
            access_key="AKIDEXAMPLE",
            secret_key="secret",
            session_token="",
            body=b'{"a":1}',
            extra_headers={"content-type": "application/json"},
        )
        self.assertIn("authorization", headers)
        auth = headers["authorization"]
        self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"))
        self.assertIn("/eu-central-1/bedrock/aws4_request", auth)
        self.assertIn("SignedHeaders=", auth)
        self.assertIn("content-type;host;x-amz-content-sha256;x-amz-date", auth)
        self.assertIn("Signature=", auth)
        self.assertIn("x-amz-date", headers)
        self.assertNotIn("host", headers)  # left to the HTTP library

    def test_session_token_signed(self):
        headers = aws_sigv4.sign_request(
            method="POST",
            url="https://bedrock-runtime.us-east-1.amazonaws.com/model/foo/invoke",
            region="us-east-1",
            service="bedrock",
            access_key="AKID",
            secret_key="secret",
            session_token="TOKEN",
            body=b"{}",
        )
        self.assertEqual(headers["x-amz-security-token"], "TOKEN")
        self.assertIn("x-amz-security-token", headers["authorization"])


class TestMedia(unittest.TestCase):
    def test_extract_image_filenames(self):
        html = (
            '<div><img src="anat%20omie.jpg"><img src="x.png" class="io">'
            '<img src="https://example.com/remote.png"><img src="data:image/png;base64,AA==">'
            '<img src="x.png"></div>'
        )
        self.assertEqual(
            media.extract_image_filenames(html), ["anat omie.jpg", "x.png"]
        )

    def test_guess_media_type(self):
        self.assertEqual(media.guess_media_type("Bild.JPG"), "image/jpeg")
        self.assertEqual(media.guess_media_type("a.webp"), "image/webp")
        self.assertIsNone(media.guess_media_type("vektor.svg"))

    def test_load_images_caps_and_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.png", "b.png"):
                with open(os.path.join(tmp, name), "wb") as f:
                    f.write(b"\x89PNG fake")
            imgs = media.load_images(["a.png", "fehlt.png", "b.png", "c.svg"], tmp, max_images=1)
            self.assertEqual(len(imgs), 1)
            self.assertEqual(imgs[0][0], "image/png")

    def test_parse_occlusion_rect(self):
        field = (
            "{{c1::image-occlusion:rect:left=.2077:top=.4025:width=.1226:height=.0705:oi=1}}"
            "{{c2::image-occlusion:rect:left=.5:top=.5:width=.1:height=.1:oi=1}}"
        )
        shapes = media.parse_occlusion_field(field, 1)
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0]["shape"], "rect")
        self.assertAlmostEqual(shapes[0]["left"], 0.2077)
        self.assertEqual(media.parse_occlusion_field(field, 3), [])

    def test_parse_occlusion_polygon_and_hint(self):
        field = "{{c1::image-occlusion:polygon:points=.1,.2 .3,.4 .2,.6:oi=1}}"
        shapes = media.parse_occlusion_field(field, 1)
        self.assertEqual(shapes[0]["points"], [(0.1, 0.2), (0.3, 0.4), (0.2, 0.6)])
        hint = media.occlusion_hint(shapes)
        self.assertIn("polygon", hint)
        self.assertIn("%", hint)

    def test_occlusion_hint_rect(self):
        hint = media.occlusion_hint(
            [{"shape": "rect", "left": 0.2, "top": 0.4, "width": 0.2, "height": 0.1}]
        )
        self.assertIn("30% von links", hint)  # center x = 0.2 + 0.1
        self.assertIn("45% von oben", hint)

    def test_occlusion_hint_pixel_format(self):
        hint = media.occlusion_hint(
            [{"shape": "rect", "left": 120, "top": 80, "width": 40, "height": 20}]
        )
        self.assertIn("px", hint)

    def test_user_content_with_images(self):
        content = grader.build_user_content("F", "B", "A", images=[("image/png", "QUJD")])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[-1]["type"], "text")
        self.assertIn("beigefügt", content[-1]["text"])

    def test_user_content_io_hint(self):
        content = grader.build_user_content("F", "B", "A", io_hint="rect bei 30%/45%")
        self.assertIsInstance(content, str)
        self.assertIn("IMAGE-OCCLUSION", content)
        self.assertIn("rect bei 30%/45%", content)

    def test_explain_mode_message(self):
        text = grader.build_user_message("F", "B", "", explain_mode=True)
        self.assertIn("WEISS DIE ANTWORT NICHT", text)
        self.assertNotIn("ANTWORT DES LERNENDEN", text)
        self.assertIn("KARTENRÜCKSEITE", text)
        normal = grader.build_user_message("F", "B", "meine Antwort")
        self.assertIn("ANTWORT DES LERNENDEN", normal)
        self.assertNotIn("WEISS DIE ANTWORT NICHT", normal)

    def test_user_content_plain(self):
        content = grader.build_user_content("F", "B", "A")
        self.assertIsInstance(content, str)
        self.assertNotIn("IMAGE-OCCLUSION", content)


class TestPromptBuilding(unittest.TestCase):
    def test_cache_control_on_script_block(self):
        blocks = grader.build_system_blocks("Deutsch", "Skripttext hier")
        self.assertEqual(len(blocks), 2)
        self.assertNotIn("cache_control", blocks[0])
        self.assertEqual(blocks[1]["cache_control"], {"type": "ephemeral"})

    def test_no_script_no_second_block(self):
        blocks = grader.build_system_blocks("Deutsch", None)
        self.assertEqual(len(blocks), 1)

    def test_rules_mention_conservative_mapping(self):
        rules = grader.build_rules_prompt("Deutsch")
        for token in ("Again", "Hard", "Good", "Easy", "NIEDRIGERE"):
            self.assertIn(token, rules)

    def test_custom_rules_replace_default_but_keep_format(self):
        prompt = grader.build_rules_prompt("Deutsch", "Du bist ein milder Tutor.")
        self.assertIn("milder Tutor", prompt)
        self.assertNotIn("NIEDRIGERE", prompt)  # default rules replaced
        self.assertIn("AUSGABEFORMAT", prompt)  # JSON contract always appended
        self.assertIn('"score"', prompt)

    def test_custom_rules_empty_falls_back_to_default(self):
        self.assertEqual(
            grader.build_rules_prompt("Deutsch", "   "),
            grader.build_rules_prompt("Deutsch"),
        )

    def test_language_placeholder_and_braces_safe(self):
        prompt = grader.build_rules_prompt(
            "Englisch", 'Antworte auf {language}. Beispiel: {"kein": "crash"}'
        )
        self.assertIn("Antworte auf Englisch.", prompt)
        self.assertIn('{"kein": "crash"}', prompt)  # user braces survive
        self.assertNotIn("{language}", prompt)

    def test_custom_rules_flow_into_system_blocks(self):
        blocks = grader.build_system_blocks("Deutsch", None, custom_rules="Sei sehr streng.")
        self.assertIn("Sei sehr streng.", blocks[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
