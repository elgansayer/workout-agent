"""Regression tests for the deployed anonymous-access smoke probe."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from webapp.auth_boundary import is_public_path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _BACKEND_ROOT / "tools" / "anonymous_access_smoke.py"

_INSPECT_POLICY = """
import importlib.util
import json

spec = importlib.util.spec_from_file_location("smoke", {module_path!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
routes, policy = module._load_route_inventory()
paths = [
    "/api/future-export.json",
    "/reports/user-chart.svg",
    "/logout",
    "/google-health/callback",
    "/main-ABCDEF12.js",
    "/sw.js",
]
print(
    json.dumps(
        {{
            "route_count": len(routes),
            "has_suffixes": hasattr(module, "PUBLIC_SUFFIXES"),
            "public": {{path: policy(path) for path in paths}},
        }}
    )
)
"""


def _load_smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_anonymous_access_smoke_under_test",
        _MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_uses_the_canonical_route_policy() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WEB_", "ALLOW_ANONYMOUS_WEB", "APP_ENV"))
    }
    environment.update(
        {
            "PYTHONPATH": str(_BACKEND_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _INSPECT_POLICY.format(module_path=str(_MODULE_PATH)),
        ],
        cwd=_BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["route_count"] > 0
    assert data["has_suffixes"] is False
    assert data["public"] == {
        "/api/future-export.json": False,
        "/reports/user-chart.svg": False,
        "/logout": False,
        "/google-health/callback": False,
        "/main-ABCDEF12.js": True,
        "/sw.js": True,
    }


def test_probe_checks_a_suffix_bearing_personalised_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_smoke_module()
    requested: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            body = b'{"refresh_token":"must-never-be-public"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    routes = [
        SimpleNamespace(path="/main-ABCDEF12.js", methods={"GET"}),
        SimpleNamespace(path="/api/future-export.json", methods={"GET"}),
    ]
    monkeypatch.setattr(
        module,
        "_load_route_inventory",
        lambda: (routes, is_public_path),
    )
    try:
        failures = module._probe(
            f"http://127.0.0.1:{server.server_port}",
            timeout=2.0,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert requested == ["/api/future-export.json"]
    assert failures
    assert "returned 200" in failures[0]
    assert "future-export.json" in failures[0]
