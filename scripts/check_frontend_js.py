#!/usr/bin/env python
"""Structural check on the inline JavaScript in the static pages.

The UI is inline `<script>` inside HTML templates: no bundler, no linter, no
build step. A stray brace therefore ships a blank dashboard with nothing to
catch it. This is not a JavaScript parser, but it is a real tokenizer for the
parts that matter, so it catches the realistic damage from editing those
blocks — unbalanced braces, parentheses or brackets, an unterminated template
literal, a newline inside a quoted string.

    python scripts/check_frontend_js.py

Exits non-zero on a problem, so it can run in CI beside the test suite.

Two things have to be handled properly or the output is noise, and both were
learned by this script reporting things that were not true:

* **Regex literals.** Every page's `esc()` helper is built on `/[&<>"']/g`.
  Treating that `/` as division (or the `"` inside it as a string) produced five
  identical false reports — including in a file that had not been edited, which
  is what gave the bug away.
* **Nested template literals.** `` `a ${items.map(x => `<li>${x}</li>`)} b` ``
  is ordinary in this codebase. Scanning a template literal by looking only for
  the next backtick closes the outer one on the inner one and desynchronises
  everything after it, which is where the second round of false reports came
  from. So `${ ... }` pushes a real expression context and the matching `}` pops
  back into the template.
"""

from __future__ import annotations

import io
import pathlib
import re
import sys

BACKSLASH = chr(92)
STATIC = pathlib.Path(__file__).resolve().parent.parent / "src" / "reviewbot" / "api" / "static"

_OPENERS = {"{": "}", "(": ")", "[": "]"}
_CLOSERS = {"}": "{", ")": "(", "]": "["}

# A `/` starts a regex literal rather than a division when the previous
# significant character cannot end an expression.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>") | {""}


class _Scanner:
    """Character-by-character scan with an explicit context stack."""

    def __init__(self, js: str) -> None:
        self.js = js
        self.n = len(js)
        self.i = 0
        self.problems: list[str] = []
        # Each frame is ("code", depth_counts) or ("template",) — a `${` inside a
        # template pushes a "code" frame whose brace depth tells us which `}`
        # ends the substitution rather than an object literal.
        self.stack: list[dict] = [{"kind": "code", "braces": 0, "parens": 0, "brackets": 0}]
        self.previous = ""

    # -- helpers ---------------------------------------------------------

    @property
    def frame(self) -> dict:
        return self.stack[-1]

    def _at(self, offset: int = 0) -> str:
        index = self.i + offset
        return self.js[index] if 0 <= index < self.n else ""

    def _note(self, message: str) -> None:
        self.problems.append(f"{message} at offset {self.i}")

    # -- the scan --------------------------------------------------------

    def run(self) -> list[str]:
        while self.i < self.n:
            char = self._at()
            following = self._at(1)

            # The template-literal branch MUST come first. Template bodies are
            # HTML, full of `</div>` and quotes; testing those for regex-ness or
            # string-ness before knowing we are inside a template is what made an
            # earlier version of this script report dozens of imaginary errors.
            if self.frame["kind"] == "template":
                if char == BACKSLASH:
                    self.i += 2
                    continue
                if char == "$" and following == "{":
                    self.i += 2
                    self.stack.append(
                        {"kind": "code", "braces": 0, "parens": 0, "brackets": 0, "substitution": True}
                    )
                    continue
                if char == "`":
                    self.i += 1
                    self.stack.pop()
                    self.previous = "`"
                    continue
                self.i += 1
                continue

            if char == "/" and following == "/":
                self._skip_line_comment()
                continue
            if char == "/" and following == "*":
                self._skip_block_comment()
                continue
            if char == "/" and self.previous in _REGEX_PRECEDERS:
                self._skip_regex()
                continue
            if char in ("'", '"'):
                self._skip_string(char)
                continue
            if char == "`":
                self.i += 1
                self.stack.append({"kind": "template"})
                continue

            self._code_char(char)

        # Anything still open at the end is a genuine problem.
        for frame in self.stack:
            if frame["kind"] == "template":
                self.problems.append("script ends inside a template literal")
            else:
                for key, symbol in (("braces", "{"), ("parens", "("), ("brackets", "[")):
                    if frame.get(key):
                        self.problems.append(f"{frame[key]:+d} unbalanced {symbol!r}")
        return self.problems

    def _code_char(self, char: str) -> None:
        frame = self.frame
        if char == "{":
            frame["braces"] += 1
        elif char == "}":
            if frame.get("substitution") and frame["braces"] == 0:
                # End of a `${ ... }`; back into the enclosing template.
                self.stack.pop()
                self.i += 1
                self.previous = "}"
                return
            frame["braces"] -= 1
            if frame["braces"] < 0:
                self._note("unmatched '}'")
                frame["braces"] = 0
        elif char == "(":
            frame["parens"] += 1
        elif char == ")":
            frame["parens"] -= 1
            if frame["parens"] < 0:
                self._note("unmatched ')'")
                frame["parens"] = 0
        elif char == "[":
            frame["brackets"] += 1
        elif char == "]":
            frame["brackets"] -= 1
            if frame["brackets"] < 0:
                self._note("unmatched ']'")
                frame["brackets"] = 0

        if not char.isspace():
            self.previous = char
        self.i += 1

    def _skip_line_comment(self) -> None:
        while self.i < self.n and self._at() != "\n":
            self.i += 1

    def _skip_block_comment(self) -> None:
        self.i += 2
        while self.i < self.n and not (self._at() == "*" and self._at(1) == "/"):
            self.i += 1
        self.i += 2

    def _skip_string(self, quote: str) -> None:
        self.i += 1
        while self.i < self.n:
            char = self._at()
            if char == BACKSLASH:
                self.i += 2
                continue
            if char == quote:
                self.i += 1
                self.previous = quote
                return
            if char == "\n":
                # A raw newline inside a quoted string is a syntax error in JS.
                self._note(f"unterminated {quote} string")
                return
            self.i += 1
        self._note(f"unterminated {quote} string")

    def _skip_regex(self) -> None:
        self.i += 1
        in_class = False
        while self.i < self.n:
            char = self._at()
            if char == BACKSLASH:
                self.i += 2
                continue
            if char == "\n":
                self._note("unterminated regex")
                return
            if in_class:
                if char == "]":
                    in_class = False
            elif char == "[":
                in_class = True
            elif char == "/":
                self.i += 1
                # Skip the flags so `g` is not read as the next token.
                while self.i < self.n and self._at().isalpha():
                    self.i += 1
                self.previous = "/"
                return
            self.i += 1
        self._note("unterminated regex")


def scan(js: str) -> list[str]:
    return _Scanner(js).run()


def main() -> int:
    if not STATIC.is_dir():
        print(f"no static directory at {STATIC}", file=sys.stderr)
        return 1

    failed = False
    checked = 0

    for path in sorted(STATIC.glob("*.html")):
        html = io.open(path, encoding="utf-8").read()
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
        problems: list[str] = []
        for block in blocks:
            problems += scan(block)
        checked += 1
        print(f"{path.name:24} {len(blocks)} script block(s)  {'OK' if not problems else 'PROBLEMS'}")
        for problem in problems:
            print(f"    {problem}")
            failed = True

    for path in sorted(STATIC.glob("*.js")):
        problems = scan(io.open(path, encoding="utf-8").read())
        checked += 1
        print(f"{path.name:24} standalone         {'OK' if not problems else 'PROBLEMS'}")
        for problem in problems:
            print(f"    {problem}")
            failed = True

    print(f"\n{checked} file(s) checked, {'problems found' if failed else 'all balanced'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
