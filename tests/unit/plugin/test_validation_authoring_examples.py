"""Syntax integrity of the worked Python examples in the end-user plugin's docs.

``plugin/skills/dataspoke-validation/references/validation-authoring.md`` is the flagship
authoring guide agents read while writing validation into a user's pipeline. It embeds several
fenced Python code blocks — a full worked module, a call-site wiring snippet, per-engine
``_measure_partition`` variants, and a bucketing snippet — that agents copy and adapt verbatim
into a real pipeline. Nothing checks that these blocks are even syntactically valid Python: they
live inside prose, so a typo introduced by a future hand-edit (a stray comma, a missing colon, an
unbalanced bracket) would be invisible until an agent actually tried to run the adapted code
against a user's data. This file protects that flagship worked example — and every other Python
example under ``plugin/``, present or future — from a silent syntax break by parsing every
embedded Python block with ``ast.parse``.

The scan is not pinned to ``validation-authoring.md`` alone, nor to an exact ` ```python ` fence
label: it walks every ``.md`` file under ``plugin/`` and accepts an optional ``py``/``python``
fence variant with trailing info-string text (e.g. ` ```python title="..." `), so a Python
example added to a different doc, or a fence relabeled ` ```py `, is not silently invisible to
this check. A dedicated backstop still asserts ``validation-authoring.md`` itself — the flagship
document this file exists to protect — yields at least one block, so losing coverage on it
specifically fails loudly even if other docs' blocks are still found.

This is a pure syntax check, not an execution or import check — the blocks are illustrative and
reference names that only exist in the surrounding narrative (``spark``, ``DEST``, ``TABLE``,
``hist``) or in the pipeline they get adapted into. They must never be executed, only parsed.

No ``Spec:`` citation applies here — this file does not verify spec-defined product behavior, it
verifies that illustrative documentation stays syntactically valid Python. Fabricating a spec
citation for a documentation-integrity check would violate this project's citation-discipline
rule (spec/TESTING.md §Assertion Discipline; scaffold/roles/test.md test-quality checklist item
4: "Every `spec:` citation references text that actually exists in the cited document and
section"), so this docstring states the reason plainly instead.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# tests/unit/plugin/test_validation_authoring_examples.py → parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugin"
VALIDATION_AUTHORING_MD = (
    PLUGIN_ROOT / "skills" / "dataspoke-validation" / "references" / "validation-authoring.md"
)

#: Opens on a fence line of its own; accepts ``python`` or ``py``, plus any trailing info-string
#: text (e.g. a title attribute) after the language tag. The block body runs until the next
#: closing fence line.
_PYTHON_FENCE_RE = re.compile(r"^```(?:python|py)\b.*$", re.MULTILINE)
_CLOSING_FENCE_RE = re.compile(r"^```\s*$", re.MULTILINE)


def plugin_doc_files() -> list[Path]:
    """Every Markdown document under ``plugin/`` — the scanned set for the syntax check."""
    return sorted(PLUGIN_ROOT.rglob("*.md"))


def _extract_python_blocks(
    text: str, source: Path | str = VALIDATION_AUTHORING_MD
) -> list[tuple[int, str]]:
    """Every fenced Python block in *text*, as ``(1-indexed start line, body)`` pairs.

    The start line is the line the opening fence itself sits on, so a failure names the exact
    line to open the file at — the body's own first line would point one line too low. *source*
    is used only to name the file in the ``unclosed fence`` error message.
    """
    blocks: list[tuple[int, str]] = []
    for open_match in _PYTHON_FENCE_RE.finditer(text):
        body_start = open_match.end() + 1  # skip the newline ending the fence line
        close_match = _CLOSING_FENCE_RE.search(text, body_start)
        if close_match is None:
            raise ValueError(
                f"unclosed python fence opened at offset {open_match.start()} in {source}"
            )
        body = text[body_start : close_match.start()]
        start_line = text.count("\n", 0, open_match.start()) + 1
        blocks.append((start_line, body))
    return blocks


def python_blocks() -> list[tuple[Path, int, str]]:
    """Every fenced Python block found across ``plugin_doc_files()``, as
    ``(source_file, start_line, body)`` triples."""
    blocks: list[tuple[Path, int, str]] = []
    for path in plugin_doc_files():
        text = path.read_text(encoding="utf-8")
        blocks.extend(
            (path, start_line, body) for start_line, body in _extract_python_blocks(text, path)
        )
    return blocks


def _python_block_params() -> list[object]:
    return [
        pytest.param(source_file, start_line, body, id=f"{source_file.name}:line-{start_line}")
        for source_file, start_line, body in python_blocks()
    ]


class TestExtractionIsNonEmpty:
    """Backstop: an empty parametrize list would collect zero tests and pass vacuously."""

    def test_python_blocks_are_found(self) -> None:
        blocks = python_blocks()
        assert blocks, (
            f"No fenced Python blocks found under {PLUGIN_ROOT} — either the doc set's code "
            f"fences changed shape or the extraction regex drifted, and the syntax check below "
            f"has nothing to verify."
        )

    def test_validation_authoring_md_specifically_yields_at_least_one_block(self) -> None:
        """The flagship document this file exists to protect. Asserted on its own so losing
        coverage on it specifically fails loudly even if some other doc's blocks are still
        found by the general scan above."""
        blocks = [
            (start_line, body)
            for source_file, start_line, body in python_blocks()
            if source_file == VALIDATION_AUTHORING_MD
        ]
        assert blocks, (
            f"No fenced Python blocks found in "
            f"{VALIDATION_AUTHORING_MD.relative_to(REPO_ROOT)} — either the file's code fences "
            f"changed shape or the extraction regex drifted, and the syntax check below has "
            f"nothing to verify for the flagship authoring guide specifically."
        )


class TestExtractorHasTeeth:
    """Prove the extractor's line-counting and fence-matching can actually fail or diverge.

    Run only against synthetic text, never the real file — proves the mechanism, not the
    document's current content.
    """

    def test_extracts_a_single_block_at_its_fence_line(self) -> None:
        text = "intro\n\n```python\nx = 1\n```\n\noutro\n"
        blocks = _extract_python_blocks(text)
        assert blocks == [(3, "x = 1\n")]

    def test_extracts_multiple_blocks_at_distinct_line_numbers(self) -> None:
        text = "a\n```python\nx = 1\n```\nb\n```python\ny = 2\n```\n"
        blocks = _extract_python_blocks(text)
        assert [line for line, _ in blocks] == [2, 6]
        assert [body for _, body in blocks] == ["x = 1\n", "y = 2\n"]

    def test_raises_on_an_unclosed_fence(self) -> None:
        text = "```python\nx = 1\n"
        with pytest.raises(ValueError, match="unclosed"):
            _extract_python_blocks(text)

    def test_ignores_a_differently_labelled_fence(self) -> None:
        text = "```json\n{}\n```\n"
        assert _extract_python_blocks(text) == []

    def test_accepts_a_py_fence_with_trailing_info_string_text(self) -> None:
        """A future relabel to ` ```py ` or a trailing info-string attribute (e.g. a title)
        must not silently drop out of the scan."""
        text = 'a\n```py title="example"\nx = 1\n```\n'
        blocks = _extract_python_blocks(text)
        assert blocks == [(2, "x = 1\n")]


class TestPluginPythonExamplesParseAsPython:
    """Every fenced Python example under ``plugin/`` is syntactically valid.

    Parametrized per block, identified by its source file and starting line number, so a
    ``SyntaxError`` names the exact snippet and where to find it rather than reporting "some
    block failed."
    """

    @pytest.mark.parametrize("source_file, start_line, body", _python_block_params())
    def test_block_parses(self, source_file: Path, start_line: int, body: str) -> None:
        try:
            ast.parse(body)
        except SyntaxError as exc:
            pytest.fail(
                f"{source_file.relative_to(REPO_ROOT)}: the fenced Python block opening at "
                f"line {start_line} does not parse as valid Python ({exc}). This is a "
                f"syntax-only check — the block is never executed."
            )
