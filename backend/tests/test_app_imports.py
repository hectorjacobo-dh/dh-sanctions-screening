"""The application must actually import and expose its routes.

This exists because a syntax error in a router once got through a completely
green test run: nothing in the suite imported the FastAPI app, so
``runs.py`` failing to parse was invisible to pytest and visible only as a
server that refused connections. A test suite that passes while the service
cannot start is worse than no test at all — it is a false assurance.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def test_the_application_imports() -> None:
    from app.main import app

    assert app.title


@pytest.mark.parametrize(
    "module",
    [f"app.routers.{m.name}" for m in pkgutil.iter_modules(__import__("app.routers", fromlist=["x"]).__path__)],
)
def test_every_router_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_the_routes_the_interface_calls_are_registered() -> None:
    """A renamed or dropped route shows up as a dead button, not an error."""
    from app.main import app

    registered = {getattr(route, "path", "") for route in app.routes}
    for path in (
        "/api/health",
        "/api/search",
        "/api/watchlists/status",
        "/api/watchlists/countries",
        "/api/watchlists/snapshot.json",
        "/api/runs",
        "/api/runs/template.csv",
        "/api/runs/template.xlsx",
        "/api/runs/{run_ref}/report.csv",
        "/api/runs/{run_ref}/due-diligence-record.pdf",
    ):
        assert path in registered, f"route {path} is not registered"


def test_the_removed_txt_export_is_really_gone() -> None:
    from app.main import app

    registered = {getattr(route, "path", "") for route in app.routes}
    assert "/api/runs/{run_ref}/due-diligence-record.txt" not in registered


def test_no_test_is_shadowed_by_a_later_definition() -> None:
    """Two tests with the same name means the first one never runs.

    ``test_matching.py`` accumulated 33 shadowed definitions through repeated
    appends. Python keeps the last, so a third of that file was dead while the
    suite reported green — the same false assurance as the app never being
    imported.
    """
    import ast
    import collections
    import pathlib

    offenders: dict[str, dict[str, int]] = {}
    for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        duplicated = {n: c for n, c in collections.Counter(names).items() if c > 1}
        if duplicated:
            offenders[path.name] = duplicated
    assert not offenders, f"shadowed test definitions (earlier copies never run): {offenders}"
