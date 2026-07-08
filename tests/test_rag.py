from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from reviewbot.api import rag


SOURCES = [
    rag.Source(
        text="The app crashes on login but pickup is fast.",
        source="google_play",
        source_url="https://play.example/review/1",
        author="Ava",
        rating=2,
        brand="Blue Bottle",
        sentiment="negative",
        score=0.9,
    )
]


class RagTests(unittest.TestCase):
    def test_missing_openai_key_uses_extractive_fallback(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False), patch.object(
            rag, "retrieve", return_value=SOURCES
        ):
            result = rag.answer("What breaks?", brand="Blue Bottle")

        self.assertIn("OpenAI generation is not available", result.answer)
        self.assertIn("[1]", result.answer)
        self.assertEqual(result.sources, SOURCES)

    def test_openai_response_path(self) -> None:
        captured = {}

        class FakeResponses:
            def create(self, **payload):
                captured.update(payload)
                return types.SimpleNamespace(output_text="People mention crashes [1].")

        class FakeOpenAI:
            def __init__(self):
                self.responses = FakeResponses()

        fake_module = types.SimpleNamespace(OpenAI=FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_module}), patch.dict(
            "os.environ", {"OPENAI_API_KEY": "test"}, clear=False
        ), patch.object(rag, "retrieve", return_value=SOURCES):
            result = rag.answer("What breaks?", brand="Blue Bottle")

        self.assertEqual(result.answer, "People mention crashes [1].")
        self.assertEqual(captured["model"], rag.OPENAI_MODEL)
        self.assertIn("instructions", captured)


if __name__ == "__main__":
    unittest.main()
