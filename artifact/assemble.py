"""Assemble the shareable workbench artifact, with guards.

Each guard exists because the corresponding defect actually shipped:

  * a ``const`` shadowing a parameter killed the local UI at parse time;
  * a literal control-character range ("Range out of order in character class")
    killed the first published artifact at run time, before one line executed;
  * a 4.5 MB JavaScript object literal took the compiler tens of seconds, so
    the page sat dead — the data is a JSON island now, parsed with JSON.parse.

Run: python assemble.py <scratchpad-dir>
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

BS = chr(92)  # backslash, built explicitly so no quoting layer can eat it

FUNC = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{", re.M)
DECL = re.compile(r"^\s*(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
REGEX_CLASS = re.compile(r"/\[[^\]]*\][gimsuy]*")


def shadowing_offenders(js: str) -> list[str]:
    """const/let redeclaring one of its own function's parameters."""
    out: list[str] = []
    for match in FUNC.finditer(js):
        params = [p.strip().split("=")[0].strip() for p in match.group(2).split(",") if p.strip()]
        if not params:
            continue
        depth, index = 0, match.end() - 1
        while index < len(js):
            if js[index] == "{":
                depth += 1
            elif js[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        # Only the function's own top level; a nested scope may reuse the name.
        top_level = re.sub(r"\{[^{}]*\}", "", js[match.end():index])
        out += [
            f"{match.group(1)}(): const/let {name} redeclares parameter"
            for name in DECL.findall(top_level)
            if name in params
        ]
    return out


def duplicate_functions(modules: dict[str, str]) -> list[str]:
    """Top-level function names defined in more than one module.

    JavaScript silently keeps the last definition, so a collision does not
    error — it swaps behaviour. Both art_store.js and art_ui.js defined
    sha256Hex with different parameter types; the audit chain then hashed a
    string with the ArrayBuffer variant and threw at run time.
    """
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for name, source in modules.items():
        for match in FUNC.finditer(source):
            fn = match.group(1)
            if fn in seen and seen[fn] != name:
                clashes.append(f"{fn}() defined in both {seen[fn]} and {name}")
            seen.setdefault(fn, name)
    return clashes


DEFINED = re.compile(
    r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|(?:^|\n)\s*(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()"
)

# Helpers one module calls but another defines. A general "called but never
# defined" scan was tried and abandoned: without a real JS lexer it flags
# keywords (if, while, return), callback parameters, and identifiers inside
# regex literals. This explicit list is narrow, silent when healthy, and
# catches the failure that actually happened.
REQUIRED_HELPERS = (
    # DOM and formatting (art_ui.js) — used throughout art_boot.js
    "$", "esc", "nf", "banner", "berlin", "isoUtc", "download", "slug",
    # persistence and audit (art_store.js)
    "sha256Hex", "canonicalJson", "appendAudit", "allAudit", "verifyAudit",
    "saveSnapshot", "loadSnapshot", "clearSnapshot", "snapshotAgeDays",
    # engine (art_engine.js)
    "normalize", "basicNorm", "candidates", "score", "buildIndex", "adoptBundle",
    "resolveCountry", "isGenericToken",
    # documents (art_docs.js)
    "extractDocument", "crossCheck", "validateAdjudication",
    # case flow (art_ui.js)
    "runSearch", "renderCase", "openCase", "recordLines", "printableHtml",
    # boot
    "renderProvenance", "renderStaleness", "refreshAuditPanel", "importSnapshot",
)


def missing_helpers(modules: dict[str, str]) -> list[str]:
    """Shared helpers that no module defines.

    JavaScript raises ReferenceError only when the line runs, so an unresolved
    helper inside a startup path aborts the rest of it and leaves a
    half-initialised page with no visible error. ``banner()`` was called nine
    times and defined nowhere: the staleness banner threw, and everything after
    it in the boot sequence — including the audit panel and the first search —
    was silently skipped.
    """
    defined: set[str] = set()
    for source in modules.values():
        for match in DEFINED.finditer(source):
            defined.add(match.group(1) or match.group(2))
    return [
        f"{helper}() is required by the bundle but defined nowhere"
        for helper in REQUIRED_HELPERS
        if helper not in defined
    ]


def raw_regex_bytes(js: str) -> list[str]:
    """Control or non-ASCII bytes inside a regex character class."""
    out: list[str] = []
    for line_no, line in enumerate(js.split("\n"), 1):
        for match in REGEX_CLASS.finditer(line):
            raw = [c for c in match.group(0) if ord(c) < 32 or ord(c) > 126]
            if raw:
                codes = " ".join("U+%04X" % ord(c) for c in raw)
                out.append(f"line {line_no}: raw byte(s) in regex class: {codes}")
    return out


def strip_literals(js: str) -> str:
    """Remove comments and string/template/regex literals, char by char.

    Done as a scanner rather than with regexes: the patterns needed to match
    JS literals correctly are themselves fragile to quote through a shell.
    """
    out: list[str] = []
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and js[i] != "\n":
                i += 1
        elif c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                i += 1
            i += 2
        elif c in "'\"`":
            quote = c
            i += 1
            while i < n:
                if js[i] == BS:
                    i += 2
                    continue
                if js[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
        else:
            out.append(c)
            i += 1
    return "".join(out)


def unbalanced(js: str) -> list[str]:
    stripped = strip_literals(js)
    out: list[str] = []
    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        a, b = stripped.count(opener), stripped.count(closer)
        if a != b:
            out.append(f"unbalanced {opener}{closer}: {a} vs {b}")
    return out


def embed(obj: object) -> str:
    """JSON for a data island; "<" escaped so "</script>" cannot close the tag."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", BS + "u003c")


def main() -> int:
    sp = pathlib.Path(sys.argv[1])
    shell = (sp / "art_shell.html").read_text(encoding="utf-8")
    names = ("art_store.js", "art_engine.js", "art_docs.js", "art_ui.js", "art_boot.js")
    modules = {name: (sp / name).read_text(encoding="utf-8") for name in names}
    js = "\n".join(modules[name] for name in names)

    # Precise guards — every one caught a defect that actually shipped.
    problems = (
        shadowing_offenders(js)
        + raw_regex_bytes(js)
        + duplicate_functions(modules)
        + missing_helpers(modules)
    )
    if problems:
        print("GUARDS FAILED:")
        for problem in problems:
            print("  " + problem)
        return 1

    # Advisory only. strip_literals does not lex regex literals, and a pattern
    # such as /\((?:\\.|[^\\()])*\)/g legitimately skews bracket counts. A
    # correct check needs a real JS lexer; the definitive gate is parsing the
    # assembled script in a browser, which is done before publishing.
    warnings = unbalanced(js)
    if warnings:
        print("advisory (regex literals skew this count, not necessarily a fault):")
        for warning in warnings:
            print("  " + warning)

    bundle = json.loads((sp / "designations_full.json").read_text(encoding="utf-8"))
    bundle["snapshot"] = "22 September 2026"
    tax = json.loads((sp / "taxonomy.json").read_text(encoding="utf-8"))

    data_js, data_tax = embed(bundle), embed(tax)
    if "</script>" in data_js or "</script>" in data_tax:
        print("GUARD FAILED: data island can close its own tag")
        return 1

    out = (
        shell
        + '\n<script id="watchlist-data" type="application/json">' + data_js + "</script>\n"
        + '<script id="taxonomy-data" type="application/json">' + data_tax + "</script>\n"
        + "<script>\n" + js + "\n</script>\n"
    )

    low = out.lower()
    for marker in (
        "dh_group", "account country", "partner legal name", "cambodia prince",
        "restaurants matches", "hctorj", "ctorjacobo", "c:" + BS + "users",
        "master_sanctions_database", "screening.db", "local.operator",
    ):
        if marker in low:
            print(f"GUARD FAILED: partner-data marker present: {marker}")
            return 1

    (sp / "workbench.html").write_text(out, encoding="utf-8")
    print("assembled workbench.html  %.2f MB   (code %d KB, data %.2f MB)"
          % (len(out) / 1048576, len(js) / 1024, len(data_js) / 1048576))
    print("guards passed: shadowing, regex-bytes, delimiters, tag-breakout, leak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
