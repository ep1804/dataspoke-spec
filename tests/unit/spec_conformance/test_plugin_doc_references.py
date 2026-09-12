"""Cross-document reference integrity for the end-user plugin's ``references/`` split.

``plugin/`` is the shippable End-User AI Scaffold (``spec/AI_PLUGIN.md``). Its two largest
skills (``dataspoke-validation``, ``dataspoke-governance``) were split into thin ``SKILL.md``
routers plus skill-local ``references/*.md`` documents, and a ``plugin/references/pagination.md``
is shared across every skill. Nothing under ``tests/`` checks that the many cross-references this
split created — markdown links between documents, and bare backtick-quoted path mentions inside
router prose — actually resolve. A rename, a moved file, or a typo'd relative path in a future
hand-edit would be invisible: the body still reads fine, the skill still loads, and only an agent
that actually follows the broken pointer at runtime would notice.

Two independent reference conventions are checked, both declared in
``spec/AI_PLUGIN.md`` §Architecture → Packaging:

* **Markdown links** — ``[`label`](relative/path.md)`` — resolved relative to the *containing
  file's own directory*, standard Markdown relative-link semantics. This is why
  ``[`plugin/references/pagination.md`](../../../references/pagination.md)`` inside
  ``skills/dataspoke-validation/references/validation-conf.md`` correctly climbs three levels
  to ``plugin/references/pagination.md``. A markdown link is also checked for containment: an
  installed plugin ships only ``plugin/`` (not the rest of this monorepo), so a link resolving
  outside ``plugin/`` would dangle for an end user even though it resolves fine in this repo
  checkout — unless it is an explicitly named, reasoned exception in
  ``EXTERNAL_LINK_ALLOWLIST``.
* **Bare backtick-quoted mentions** — a path written as plain prose inside backticks, with no
  markdown link syntax, e.g. `` `references/validation-conf.md` `` inside
  ``skills/dataspoke-validation/SKILL.md``. Resolution of these follows the rule spelled out in
  spec/AI_PLUGIN.md §Architecture → Packaging: "A bare `references/…` path is skill-local
  (`skills/<skill>/references/`); the one reference shared across every skill is always written
  with its full path, `plugin/references/pagination.md`." ``plugin/README.md`` §Skill-local
  references documents the same split structurally (the ``skills/<skill>/references/`` /
  ``plugin/references/`` tree) without restating the resolution sentence itself, and is also
  checked for inventory agreement against the actual ``references/*.md`` files on disk.

Both existence checks are case-exact (an ``.exists()`` alone is case-insensitive on macOS APFS
and would pass a mistyped-case reference that breaks on a case-sensitive filesystem), and both
scanning regexes run over each file's text with fenced code blocks stripped first (an
illustrative path-shaped string inside a sample invocation is not a live cross-reference) and
URL-scheme / ``mailto:`` targets excluded from the markdown-link scan (an external link is not a
same-repo reference these resolvers reason about at all).

Spec: spec/AI_PLUGIN.md §Architecture → Packaging ("Skills use **progressive disclosure**...
detailed request shapes, error tables, and authoring patterns live in focused `references/`
documents") — the paragraph enumerating both reference conventions and the bare-path
resolution rule quoted above.
Spec: plugin/README.md §Skill-local references (not under ``spec/``, cited as a plain file
reference) — the ``skills/<skill>/references/`` and ``plugin/references/`` tree this test's
resolvers encode, and whose file-tree block is separately checked for inventory agreement.
Spec: spec/TESTING.md §Unit Testing → Scope ("Unit tests verify business logic in isolation.
They must never require a running dev environment.") — this file is pure file reads under the
repo root; no network, no dev cluster, no database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/unit/spec_conformance/test_plugin_doc_references.py → parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugin"
PLUGIN_README = PLUGIN_ROOT / "README.md"

#: ``[label](target.md...)`` — captures the whole link (label included) so masking it out
#: before the bare-mention scan removes a backtick-quoted label too, not just the target inside
#: the parentheses. Without that, a link's own label (e.g. `` [`plugin/references/pagination.md`]
#: (../../../references/pagination.md) ``) would separately re-match as a "bare" mention of the
#: same path and double-assert the identical reference under two different resolution rules.
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md[^)]*)\)")

#: A backtick-quoted path mention with no markdown link syntax around it, once markdown links
#: are masked out of the source text. Ordered as an alternation of the two forms declared in
#: spec/AI_PLUGIN.md §Architecture → Packaging: the plugin-wide reference is always written with
#: its full ``plugin/references/...`` path, everything else skill-local is a bare
#: ``references/...`` path.
BARE_MENTION_RE = re.compile(r"`(plugin/references/[\w./-]+\.md|references/[\w./-]+\.md)`")

#: A link target that is not a same-repo path at all — an external URL or a mailto: address.
#: Neither reference convention this file checks applies to these; they are excluded from the
#: markdown-link scan before resolution is even attempted.
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

#: Markdown links intentionally allowed to resolve outside ``plugin/`` — a developer-facing
#: cross-reference from inside the monorepo, not a runtime dependency any skill reads (skills
#: reach the API contract through ``dataspoke-schema``, never by reading ``spec/API.md``
#: directly). Each entry documents why the link is exempt from the plugin-containment check.
EXTERNAL_LINK_ALLOWLIST: dict[Path, frozenset[str]] = {
    # plugin/references/pagination.md points a human reader at the full API contract for
    # cases this shared summary doesn't cover; no skill opens it at runtime.
    PLUGIN_ROOT / "references" / "pagination.md": frozenset({"../../spec/API.md"}),
}

#: A tree-block top-level entry: ``skills/<skill>/`` or the shared ``references/`` directory,
#: written with no tree-drawing prefix. Sets the directory context for the file lines under it.
_TOP_DIR_RE = re.compile(r"^(skills/[\w-]+|references)/$")
#: A nested ``references/`` line under a ``skills/<skill>/`` top-level entry — marks entry into
#: that skill's own references/ folder, distinguishing its file lines from its ``SKILL.md`` line.
_NESTED_REFERENCES_DIR_RE = re.compile(r"^[├└]──\s*references/\s*$")
#: A tree-drawn file line, e.g. ``├── validation-conf.md          ← ...``. Captures just the
#: filename; the ``← ...`` annotation is not part of the match.
_TREE_FILE_RE = re.compile(r"^[├└]──\s*([\w.-]+\.md)\b")


def plugin_doc_files() -> list[Path]:
    """Every Markdown document under ``plugin/`` — the full scanned set for both checks."""
    return sorted(PLUGIN_ROOT.rglob("*.md"))


def _strip_fragment(target: str) -> str:
    """Drop a trailing ``#anchor`` or ``?query`` from a markdown-link target."""
    return re.split(r"[#?]", target, maxsplit=1)[0]


def _is_external_target(target: str) -> bool:
    """True for a link target that names a URL or a mailto: address, not a repo-relative path."""
    return bool(_URL_SCHEME_RE.match(target)) or target.startswith("mailto:")


def _strip_fenced_code_blocks(text: str) -> str:
    """Remove every ```` ``` ````-fenced code block from *text* before scanning it.

    A path-shaped string inside an illustrative fenced example (a sample CLI invocation, a
    worked snippet) is not a live cross-reference either resolver should evaluate — only prose
    is. Fences are stripped by simple open/close toggling; nothing in the current doc set relies
    on an unmatched trailing fence, so that case is not specially handled.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "".join(out)


def _exists_case_exact(path: Path) -> bool:
    """Like ``Path.exists()`` but exact on the leaf name, not case-insensitive.

    macOS APFS resolves ``.exists()`` case-insensitively by default, so a reference mistyped as
    ``Validation-Conf.md`` would pass locally and only break on a case-sensitive filesystem
    (Linux CI, most Docker images). This checks the resolved path's own directory listing for an
    exact (case-sensitive) name match instead of trusting the OS's own comparison.
    """
    if not path.parent.is_dir():
        return False
    return path.name in {entry.name for entry in path.parent.iterdir()}


def resolve_markdown_link(source_file: Path, target: str) -> Path:
    """Resolve a markdown-link target relative to *source_file*'s own directory.

    Standard Markdown relative-link semantics: the target is resolved against the directory
    the *link itself* lives in, not against ``PLUGIN_ROOT`` or the repo root.
    """
    return (source_file.parent / _strip_fragment(target)).resolve()


def resolve_bare_mention(source_file: Path, token: str) -> Path:
    """Resolve a bare backtick-quoted ``references/...`` or ``plugin/references/...`` token.

    Per spec/AI_PLUGIN.md §Architecture → Packaging: a ``plugin/``-prefixed token is the one
    reference shared across every skill and always resolves from the repo root. A bare
    ``references/...`` token is skill-local: relative to the enclosing skill's own directory
    (``skills/<skill>/``) when the mention is in that skill's ``SKILL.md`` or ``README.md``, or
    climbing one level up from the enclosing directory when the mention is inside that skill's
    own ``references/*.md`` — so a reference doc mentioning a sibling in its own folder still
    means ``skills/<skill>/references/...``, not ``skills/<skill>/references/references/...``.
    """
    if token.startswith("plugin/"):
        return (REPO_ROOT / token).resolve()
    if source_file.parent.name == "references":
        base = source_file.parent.parent
    else:
        base = source_file.parent
    return (base / token).resolve()


def _parse_readme_reference_tree() -> frozenset[str]:
    """Every reference file ``plugin/README.md``'s §Skill-local references file-tree block
    names, as paths relative to ``PLUGIN_ROOT``.

    Pragmatic line-shape parsing over the fenced tree block, not a general Markdown-tree parser:
    a top-level line (``skills/<skill>/`` or ``references/``) sets the current directory
    context; a nested ``references/`` line (meaningful only under a ``skills/<skill>/`` context)
    marks entry into that skill's own references/ folder; a tree-drawn ``*.md`` line is
    collected only while inside a references/ context, so the sibling ``SKILL.md`` line is never
    captured. Raises when the block itself is gone, so a restructured README fails loudly rather
    than silently reducing the declared side to an empty set.
    """
    text = PLUGIN_README.read_text(encoding="utf-8")
    match = re.search(r"## Skill-local references\n.*?```\n(.*?)```", text, re.DOTALL)
    if match is None:
        raise LookupError(
            "plugin/README.md has no fenced tree block under '## Skill-local references' — "
            "it was restructured, and the reference-inventory check has no spec side."
        )
    declared: set[str] = set()
    current_top: str | None = None
    in_references = False
    for line in match.group(1).splitlines():
        stripped = line.strip()
        top_match = _TOP_DIR_RE.match(stripped)
        if top_match:
            current_top = top_match.group(1)
            in_references = current_top == "references"
            continue
        if _NESTED_REFERENCES_DIR_RE.match(stripped):
            in_references = True
            continue
        file_match = _TREE_FILE_RE.match(stripped)
        if file_match and in_references and current_top is not None:
            filename = file_match.group(1)
            if current_top == "references":
                declared.add(f"references/{filename}")
            else:
                declared.add(f"{current_top}/references/{filename}")
    return frozenset(declared)


def _on_disk_reference_files() -> frozenset[str]:
    """Every ``references/*.md`` file under ``plugin/``, as paths relative to ``PLUGIN_ROOT``.

    ``rglob("references/*.md")`` matches a ``references/`` directory at any depth — including
    ``plugin/references/`` itself (the shared reference, matched with a zero-length ``**``) and
    every ``skills/<skill>/references/`` folder — so no separate top-level glob is needed.
    """
    return frozenset(
        str(path.relative_to(PLUGIN_ROOT)) for path in PLUGIN_ROOT.rglob("references/*.md")
    )


def _markdown_link_cases() -> list[tuple[Path, str]]:
    """Every ``(source_file, link_target)`` pair found across ``plugin_doc_files()``.

    Fenced code blocks are stripped and external (URL-scheme / ``mailto:``) targets are excluded
    before matching — neither is a same-repo cross-reference this file resolves.
    """
    cases: list[tuple[Path, str]] = []
    for path in plugin_doc_files():
        text = _strip_fenced_code_blocks(path.read_text(encoding="utf-8"))
        cases.extend(
            (path, match.group(1))
            for match in MARKDOWN_LINK_RE.finditer(text)
            if not _is_external_target(match.group(1))
        )
    return cases


def _bare_mention_cases() -> list[tuple[Path, str]]:
    """Every ``(source_file, token)`` pair found after masking out markdown links.

    Masking first is what keeps a link's own backtick-quoted label from also being counted as
    an independent bare mention of the same path. Fenced code blocks are stripped first, for the
    same reason as the markdown-link scan.
    """
    cases: list[tuple[Path, str]] = []
    for path in plugin_doc_files():
        text = _strip_fenced_code_blocks(path.read_text(encoding="utf-8"))
        masked = MARKDOWN_LINK_RE.sub("", text)
        cases.extend((path, match.group(1)) for match in BARE_MENTION_RE.finditer(masked))
    return cases


def _markdown_link_params() -> list[object]:
    return [
        pytest.param(source_file, target, id=f"{source_file.name}:{target}")
        for source_file, target in _markdown_link_cases()
    ]


def _bare_mention_params() -> list[object]:
    return [
        pytest.param(source_file, token, id=f"{source_file.name}:{token}")
        for source_file, token in _bare_mention_cases()
    ]


class TestDiscoveryIsNonEmpty:
    """Backstops proving the two case lists above are not silently empty.

    ``pytest.mark.parametrize`` over an empty list collects zero tests and reports success —
    a vacuous pass that would hide a regex or glob that stopped matching anything. These run
    unconditionally, independent of the parametrized checks below.
    """

    def test_plugin_doc_files_are_found(self) -> None:
        found = plugin_doc_files()
        assert found, f"No .md files discovered under {PLUGIN_ROOT} — the glob is broken."

    def test_markdown_link_cases_are_found(self) -> None:
        cases = _markdown_link_cases()
        assert cases, (
            "No markdown-link `.md` targets found under plugin/ — either the regex or the "
            "doc set drifted, and the link-resolution check below has nothing to verify."
        )

    def test_bare_mention_cases_are_found(self) -> None:
        cases = _bare_mention_cases()
        assert cases, (
            "No bare `references/...` mentions found under plugin/ — either the regex or the "
            "doc set drifted, and the bare-mention check below has nothing to verify."
        )


class TestResolverHelpersHaveTeeth:
    """Negative controls on the resolver functions themselves, not on real files.

    The parametrized checks below run every real reference in ``plugin/`` through these same
    two functions; today every one of them resolves, so those checks alone cannot tell a
    correct existence check from an inverted one that always returns ``True``. These prove
    each resolver reports non-existence for a made-up path, and existence for a real one.
    """

    def test_markdown_link_resolver_accepts_a_real_relative_target(self) -> None:
        source = (
            PLUGIN_ROOT / "skills" / "dataspoke-validation" / "references" / "validation-conf.md"
        )
        resolved = resolve_markdown_link(source, "validation-authoring.md")
        assert resolved.exists()

    def test_markdown_link_resolver_rejects_a_broken_relative_target(self) -> None:
        source = (
            PLUGIN_ROOT / "skills" / "dataspoke-validation" / "references" / "validation-conf.md"
        )
        resolved = resolve_markdown_link(source, "does-not-exist.md")
        assert not resolved.exists()

    def test_markdown_link_containment_check_flags_an_unallowlisted_external_target(
        self,
    ) -> None:
        """Proves the containment check used by ``TestMarkdownLinksResolve`` can actually fail:
        a synthetic source/target pair that climbs above ``PLUGIN_ROOT`` resolves outside it and
        is not present in ``EXTERNAL_LINK_ALLOWLIST`` — the two conditions the real parametrized
        test relies on to flag an unallowlisted escape."""
        source = PLUGIN_ROOT / "skills" / "dataspoke-validation" / "SKILL.md"
        target = "../../../CLAUDE.md"
        resolved = resolve_markdown_link(source, target)
        assert not resolved.is_relative_to(PLUGIN_ROOT)
        assert target not in EXTERNAL_LINK_ALLOWLIST.get(source, frozenset())

    def test_bare_mention_resolver_accepts_a_real_skill_local_token(self) -> None:
        source = PLUGIN_ROOT / "skills" / "dataspoke-validation" / "SKILL.md"
        resolved = resolve_bare_mention(source, "references/validation-conf.md")
        assert resolved.exists()

    def test_bare_mention_resolver_rejects_a_nonexistent_skill_local_token(self) -> None:
        source = PLUGIN_ROOT / "skills" / "dataspoke-validation" / "SKILL.md"
        resolved = resolve_bare_mention(source, "references/does-not-exist.md")
        assert not resolved.exists()

    def test_bare_mention_resolver_rejects_a_nonexistent_plugin_prefixed_token(self) -> None:
        source = PLUGIN_ROOT / "skills" / "dataspoke-validation" / "SKILL.md"
        resolved = resolve_bare_mention(source, "plugin/references/does-not-exist.md")
        assert not resolved.exists()

    def test_bare_mention_resolver_resolves_a_plugin_root_token_relative_to_its_own_directory(
        self,
    ) -> None:
        """A bare ``references/...`` mention inside ``plugin/README.md`` resolves relative to
        ``README.md``'s own directory, which is ``plugin/`` itself — the ordinary
        ``else: base = source_file.parent`` path, not a special case."""
        source = PLUGIN_ROOT / "README.md"
        resolved = resolve_bare_mention(source, "references/pagination.md")
        assert resolved == (PLUGIN_ROOT / "references" / "pagination.md").resolve()
        assert resolved.exists()

    def test_bare_mention_resolver_climbs_from_a_skill_reference_doc_to_its_own_skill(
        self,
    ) -> None:
        """From inside a skill's own ``references/*.md`` doc, a bare mention of a sibling
        reference resolves via the ``references/``-climb branch to *that skill's* references/
        directory specifically — proving it reaches the right directory, not just some
        directory that happens to also contain a same-named file."""
        source = (
            PLUGIN_ROOT / "skills" / "dataspoke-validation" / "references" / "validation-conf.md"
        )
        resolved = resolve_bare_mention(source, "references/validation-authoring.md")
        assert resolved == (
            PLUGIN_ROOT
            / "skills"
            / "dataspoke-validation"
            / "references"
            / "validation-authoring.md"
        ).resolve()

    def test_bare_mention_resolver_does_not_reach_the_shared_reference_through_a_skill_local_path(
        self,
    ) -> None:
        """spec/AI_PLUGIN.md §Architecture → Packaging forbids writing the shared reference as a
        bare ``references/pagination.md`` from inside a skill doc — it must always carry its
        full ``plugin/``-prefixed path. Proves the ``references/``-climb branch does not
        accidentally make that bare form resolve to the shared plugin-wide reference anyway."""
        source = (
            PLUGIN_ROOT / "skills" / "dataspoke-validation" / "references" / "validation-conf.md"
        )
        resolved = resolve_bare_mention(source, "references/pagination.md")
        assert resolved != (PLUGIN_ROOT / "references" / "pagination.md").resolve()


class TestMarkdownLinksResolve:
    """Every ``[label](target.md)`` link under ``plugin/`` points at a real file (case-exact)
    and, unless explicitly allowlisted, stays inside ``plugin/`` itself."""

    @pytest.mark.parametrize("source_file, target", _markdown_link_params())
    def test_markdown_link_target_exists(self, source_file: Path, target: str) -> None:
        resolved = resolve_markdown_link(source_file, target)
        assert _exists_case_exact(resolved), (
            f"{source_file.relative_to(REPO_ROOT)}: markdown link target {target!r} resolves "
            f"to {resolved}, which does not exist (or differs only in case). Markdown links "
            f"resolve relative to the containing file's own directory."
        )

    @pytest.mark.parametrize("source_file, target", _markdown_link_params())
    def test_markdown_link_target_stays_inside_plugin_unless_allowlisted(
        self, source_file: Path, target: str
    ) -> None:
        allowlisted_targets = EXTERNAL_LINK_ALLOWLIST.get(source_file, frozenset())
        if target in allowlisted_targets:
            return
        resolved = resolve_markdown_link(source_file, target)
        assert resolved.is_relative_to(PLUGIN_ROOT), (
            f"{source_file.relative_to(REPO_ROOT)}: markdown link target {target!r} resolves "
            f"to {resolved}, outside plugin/. An installed plugin ships only plugin/, so this "
            f"link would be unresolvable for an end user even though it resolves in this repo "
            f"checkout. If this is an intentional developer-facing cross-reference into the "
            f"monorepo (not something a skill reads at runtime), add "
            f"(source_file, target) to EXTERNAL_LINK_ALLOWLIST with a comment stating why."
        )


class TestBareMentionsResolve:
    """Every bare backtick-quoted ``references/...`` / ``plugin/references/...`` mention
    under ``plugin/`` points at a real file (case-exact), per the skill-local resolution rule
    in spec/AI_PLUGIN.md §Architecture → Packaging."""

    @pytest.mark.parametrize("source_file, token", _bare_mention_params())
    def test_bare_mention_target_exists(self, source_file: Path, token: str) -> None:
        resolved = resolve_bare_mention(source_file, token)
        assert _exists_case_exact(resolved), (
            f"{source_file.relative_to(REPO_ROOT)}: bare mention `{token}` resolves to "
            f"{resolved}, which does not exist (or differs only in case). A `plugin/`-prefixed "
            f"token resolves from the repo root; a bare `references/...` token is skill-local."
        )


class TestReferenceInventoryMatchesReadme:
    """``plugin/README.md``'s §Skill-local references file-tree block and the actual
    ``references/*.md`` files under ``plugin/`` agree, both ways.

    Mirrors ``test_plugin_manifests.py``'s ``TestSkillSetMatchesSpec`` pattern (declared vs.
    glob-discovered, asserted with set equality rather than a subset or count check) so a
    renamed or moved reference directory is caught even though every reference the doc
    checks above scan is entirely self-referential — derived from whatever the docs currently
    say, not from an independent inventory.
    """

    def test_readme_tree_is_parsed(self) -> None:
        declared = _parse_readme_reference_tree()
        assert declared, (
            "No reference files parsed from plugin/README.md's §Skill-local references tree "
            "block — the block's shape changed and this check has no spec side."
        )

    def test_reference_files_are_discovered_on_disk(self) -> None:
        on_disk = _on_disk_reference_files()
        assert on_disk, f"No references/*.md files discovered under {PLUGIN_ROOT}."

    def test_declared_and_on_disk_reference_sets_match(self) -> None:
        declared = _parse_readme_reference_tree()
        on_disk = _on_disk_reference_files()
        assert declared == on_disk, (
            "plugin/README.md §Skill-local references and the actual references/*.md files "
            "under plugin/ disagree.\n"
            f"  declared in README but not on disk: {sorted(declared - on_disk)}\n"
            f"  on disk but not declared in README: {sorted(on_disk - declared)}"
        )
