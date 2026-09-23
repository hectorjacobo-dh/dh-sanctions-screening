"""Static guards on the served UI.

A single JavaScript syntax error stops the whole inline script from compiling,
which silently disables *every* module on the page — search, tabs, upload,
audit, all of it — with no visible error. That happened: refactoring
``downloadReport`` into ``downloadFile(url, filename)`` left a
``const url = URL.createObjectURL(blob)`` in the body, redeclaring the
parameter. ``SyntaxError: Identifier 'url' has already been declared``.

There is no JS engine in this environment, so these are targeted structural
checks for the failure modes that actually cost us a broken page, rather than a
general parser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"

# `function name(a, b){` and `async function name(a, b){`
_FUNC = re.compile(
    r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{",
    re.MULTILINE,
)
_DECL = re.compile(r"^\s*(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", re.MULTILINE)


@pytest.fixture(scope="module")
def script() -> str:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(r"<script>([\s\S]*?)</script>", html)
    assert match, "no inline <script> block found in index.html"
    return match.group(1)


def _function_bodies(script: str) -> list[tuple[str, list[str], str]]:
    """Yield (name, parameters, body) by brace-matching each function."""
    found: list[tuple[str, list[str], str]] = []
    for match in _FUNC.finditer(script):
        name = match.group(1)
        params = [
            p.strip().split("=")[0].strip()
            for p in match.group(2).split(",")
            if p.strip()
        ]
        depth, index = 0, match.end() - 1
        while index < len(script):
            char = script[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        found.append((name, params, script[match.end() : index]))
    return found


def test_no_declaration_shadows_its_own_parameter(script: str) -> None:
    """The exact bug that disabled the whole page. Never again."""
    offenders: list[str] = []
    for name, params, body in _function_bodies(script):
        if not params:
            continue
        # Only the function's own top level matters; a nested scope may legally
        # reuse the identifier.
        top_level = re.sub(r"\{[^{}]*\}", "", body)
        for declared in _DECL.findall(top_level):
            if declared in params:
                offenders.append(f"{name}(): 'const/let {declared}' redeclares parameter")
    assert not offenders, "SyntaxError-inducing shadowing:\n  " + "\n  ".join(offenders)


def test_delimiters_are_balanced(script: str) -> None:
    """Catches a stray brace or paren from a bad edit.

    String and template literals are stripped first so their contents cannot
    skew the count.
    """
    stripped = script
    stripped = re.sub(r"//[^\n]*", "", stripped)
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", stripped)
    for pattern in (r"`(?:\\.|[^`\\])*`", r"'(?:\\.|[^'\\\n])*'", r'"(?:\\.|[^"\\\n])*"'):
        stripped = re.sub(pattern, '""', stripped)

    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        assert stripped.count(opener) == stripped.count(closer), (
            f"unbalanced {opener}{closer}: "
            f"{stripped.count(opener)} vs {stripped.count(closer)}"
        )


def test_every_referenced_element_id_exists(script: str) -> None:
    """`$('id').onclick = ...` throws on null and kills the rest of the script."""
    html = INDEX.read_text(encoding="utf-8")
    declared = set(re.findall(r'id="([^"]+)"', html))
    # Ids created dynamically inside template literals, not present in static HTML.
    dynamic = {
        "btnCaseOpen", "btnCaseDossier", "btnReport", "btnDossier", "btnDossierTxt",
        "btnSave", "saveState", "savebar", "mBody", "pPrev", "pNext",
        "fRegion", "fSource", "fBand", "fScore",
    }
    referenced = set(re.findall(r"\$\('([A-Za-z_][\w-]*)'\)", script))
    missing = sorted(referenced - declared - dynamic)
    assert not missing, f"script references ids that do not exist: {missing}"


def test_no_leftover_template_placeholders(script: str) -> None:
    """A failed scripted edit can leave a marker behind."""
    for marker in ("__DESIGNATIONS__", "TODO_REPLACE", "<<<", ">>>"):
        assert marker not in script, f"leftover placeholder in the UI: {marker}"


# ---------------------------------------------------------------------------
# contract between the search template and the API it renders
# ---------------------------------------------------------------------------

# A hit field the template reads but the response model does not define renders
# as "undefined" — or, worse, as a silently empty cell that looks like the
# regulator published nothing. That is exactly the class of defect behind
# "DOB: not provided" appearing against records that do state a date of birth,
# so the mapping is asserted rather than trusted.
_HIT_FIELD = re.compile(r"\bh\.([a-z_][a-z0-9_]*)\b")

# Rendered from the response envelope or computed locally, not from a hit.
_TEMPLATE_LOCALS = {"toFixed", "slice", "map", "join", "length", "filter", "indexOf"}


def test_search_template_only_reads_fields_the_api_returns(script: str) -> None:
    from app.routers.search import SearchHit

    declared = set(SearchHit.model_fields)
    referenced = {
        name
        for name in _HIT_FIELD.findall(script)
        if name not in _TEMPLATE_LOCALS
    }
    unknown = sorted(referenced - declared)
    assert not unknown, (
        "the search results template reads hit field(s) the API never returns: "
        f"{unknown}. They would render blank, which on a sanctions record reads "
        "as 'the regulator published nothing'."
    )


def test_published_identity_fields_are_actually_rendered(script: str) -> None:
    """The record an officer adjudicates against must reach the page.

    OFAC publishes date of birth, nationality and document numbers inside its
    free-text remarks; the ingest layer parses them out. If the UI then fails
    to render a field, the data is recovered and thrown away again.
    """
    for field in ("dob", "nationality", "place_of_birth", "identifiers", "listing_reason"):
        assert f"h.{field}" in script, f"published field {field!r} is never rendered"


def test_no_signal_label_claims_the_designation_is_empty(script: str) -> None:
    """Guard the specific wording that caused the misreading.

    "not provided" does not say *who* failed to provide. Against a sanctions
    hit it reads as a statement about the designation, and an officer who
    believes the regulator holds no date of birth has lost their strongest
    discriminator — in the direction of clearing the alert.
    """
    # Comments are stripped first: the commentary explaining this very defect
    # quotes the old label, and a guard that fires on its own rationale is a
    # guard people delete.
    code = re.sub(r"/\*[\s\S]*?\*/", "", script)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
    assert "not provided" not in code, (
        'the ambiguous label "not provided" is back in the UI; state which side '
        "of the comparison is missing"
    )


def test_template_literal_backticks_are_balanced(script: str) -> None:
    """An odd backtick swallows the rest of the file as a string.

    The search results block is one large nested template literal. A missing
    backtick there does not fail locally — it consumes every following
    statement, and the page dies at parse time with no visible error, which is
    precisely how this UI broke before.
    """
    code = re.sub(r"/\*[\s\S]*?\*/", "", script)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
    # Backticks inside single/double-quoted strings would be false positives;
    # none are used in this file, asserted here so the check stays sound.
    assert "'`'" not in code and '"`"' not in code
    ticks = code.count("`") - code.count(r"\`")
    assert ticks % 2 == 0, f"odd number of template-literal backticks ({ticks})"


def test_render_helpers_called_by_the_results_table_exist(script: str) -> None:
    """Every helper the results template calls must be defined.

    JavaScript raises ReferenceError only when the line runs, so a typo in a
    helper name inside the render path shows up as an empty results area rather
    than an error — the officer sees "no matches" for a name that matched.
    """
    defined = {name for name, _, _ in _function_bodies(script)}
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", script))
    for helper in ("pubLine", "comparisonCell", "whyCell", "signalClass", "bandClass", "esc", "nf"):
        assert helper in defined, f"{helper}() is called by the results table but never defined"


def test_the_alert_table_waits_for_the_status_vocabulary(script: str) -> None:
    """``renderRun`` must have STATUSES before it draws a row.

    The status dropdown's options and the reason label are both built from
    STATUSES. It was fetched lazily — only when a review panel was opened — so
    the first render of a case produced a Status select with zero options and a
    reason of "Unknown", on alerts that were already resolved and whose risk
    level displayed correctly beside it. The record was right and the screen
    contradicted it, which is worse than either being plainly broken.
    """
    match = re.search(r"async function renderRun\(run\)\s*\{(.*?)\n\}", script, re.S)
    assert match, "renderRun not found"
    body = match.group(1)

    draws_at = body.find("page.items.map")
    awaits_at = body.find("await loadReasons()")
    assert awaits_at != -1, "renderRun does not await loadReasons()"
    assert awaits_at < draws_at, (
        "renderRun draws rows before awaiting the status vocabulary; the Status "
        "dropdown will render with no options"
    )
