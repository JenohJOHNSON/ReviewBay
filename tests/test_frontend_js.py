"""The frontend has no build step, so this is its only structural guard.

Two things are tested, and the second matters as much as the first: that the
shipped pages are structurally sound, and that the checker which says so can
actually tell. A checker that always reports OK is worse than none, and an
earlier version of this one reported dozens of imaginary errors because it did
not understand regex literals or nested template literals.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_frontend_js import STATIC, scan  # noqa: E402


class ShippedPagesTests(unittest.TestCase):
    def test_every_page_script_is_structurally_sound(self) -> None:
        import re

        pages = sorted(STATIC.glob("*.html"))
        self.assertTrue(pages, "no static pages found")
        for page in pages:
            html = page.read_text(encoding="utf-8")
            blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
            problems: list[str] = []
            for block in blocks:
                problems += scan(block)
            with self.subTest(page=page.name):
                self.assertEqual(problems, [], f"{page.name}: {problems}")

    def test_standalone_scripts_are_structurally_sound(self) -> None:
        for path in sorted(STATIC.glob("*.js")):
            with self.subTest(path=path.name):
                self.assertEqual(scan(path.read_text(encoding="utf-8")), [])


class CheckerAcceptsRealPatternsTests(unittest.TestCase):
    """Patterns this codebase genuinely uses must not be flagged."""

    CASES = {
        "esc helper built on a regex literal":
            """function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g, c => ({'&':'&amp;'}[c])); }""",
        "nested template literals":
            """const h = `<ul>${items.map(x => `<li>${esc(x.n)}</li>`).join('')}</ul>`;""",
        "html with attributes inside a template":
            """el.innerHTML = `<a href="${esc(u)}" target="_blank">open</a><div class="x"></div>`;""",
        "division, not a regex":
            """const pct = Math.round(100*(s.total||0)/max); const d = (r.duration_ms/1000).toFixed(1);""",
        "chained ternaries":
            """const l = a ? 'x' : b ? 'y' : 'z';""",
        "regex after return":
            """function f(s){ return /^[0-9]+$/.test(s); }""",
        "braces inside comments":
            """// this { is fine\n/* and this ( too */\nconst a = 1;""",
        "apostrophe inside a template literal":
            """const t = `it's fine`;""",
        "closing html tags inside a template":
            """const t = `</div></a>`;""",
    }

    def test_no_false_positives(self) -> None:
        for label, js in self.CASES.items():
            with self.subTest(case=label):
                self.assertEqual(scan(js), [], label)


class CheckerDetectsBreakageTests(unittest.TestCase):
    """If these pass silently the checker is decoration."""

    CASES = {
        "missing closing brace": "function f(){ if (x) { return 1; }",
        "extra closing paren": "const a = foo());",
        "unterminated template literal": "const h = `<div>oops;",
        "newline inside a quoted string": "const s = 'oops\nmore';",
        "unbalanced bracket": "const a = [1, 2, 3;",
        "unterminated nested template": "const h = `a ${xs.map(x => `<li>${x}</li>`).join('')} b;",
    }

    def test_each_breakage_is_reported(self) -> None:
        for label, js in self.CASES.items():
            with self.subTest(case=label):
                self.assertTrue(scan(js), f"{label} was not detected")


if __name__ == "__main__":
    unittest.main()
