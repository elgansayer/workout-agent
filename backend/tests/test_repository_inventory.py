from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "repository_inventory.py"
)
SPEC = importlib.util.spec_from_file_location("repository_inventory", MODULE_PATH)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class RepositoryInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "backend" / "webapp").mkdir(parents=True)
        (self.root / "backend" / "connectors").mkdir(parents=True)
        (self.root / "backend" / "tests").mkdir(parents=True)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / "frontend" / "src").mkdir(parents=True)

        (self.root / "backend" / "webapp" / "app.py").write_text(
            '''from fastapi import FastAPI

app = FastAPI()

@app.get("/login")
def login():
    return {}

@app.get("/api/history")
def history():
    return {}

@app.api_route("/exports/{item_id}", methods=["GET", "HEAD"])
def export(item_id: str):
    return {}
''',
            encoding="utf-8",
        )
        (self.root / "backend" / "config.py").write_text(
            '''import os
WEB_AUTH_SECRET = os.environ.get("WEB_AUTH_SECRET", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "workout.db")
''',
            encoding="utf-8",
        )
        (self.root / ".env.example").write_text(
            "WEB_AUTH_SECRET=replace-me\nDATABASE_PATH=workout.db\n",
            encoding="utf-8",
        )
        (self.root / "backend" / "connectors" / "fitbit.py").write_text(
            '''from .base import ConnectorCapabilities

class FitbitConnector:
    provider = "fitbit"
    capabilities = ConnectorCapabilities(
        authorize=True,
        metrics=frozenset({"sleep", "heart_rate"}),
    )
''',
            encoding="utf-8",
        )
        (self.root / ".github" / "workflows" / "ci.yml").write_text(
            '''name: CI
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{ secrets.CI_TOKEN }}" >/dev/null
''',
            encoding="utf-8",
        )
        (self.root / "backend" / "tests" / "test_routes.py").write_text(
            '''def test_inventory_references():
    assert "/api/history"
    assert "history"
    assert "fitbit"
    assert "FitbitConnector"
    assert "WEB_AUTH_SECRET"
    assert "CI"
''',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_discovers_and_classifies_routes(self) -> None:
        result = inventory.build_inventory(self.root)
        routes = {item["id"]: item for item in result["routes"]}
        self.assertEqual(routes["GET /login"]["authentication"], "public")
        self.assertEqual(routes["GET /login"]["sensitivity"], "public")
        history = routes["GET /api/history"]
        self.assertEqual(history["authentication"], "session-required")
        self.assertEqual(history["sensitivity"], "health-data")
        self.assertEqual(history["test_coverage"]["status"], "covered")
        self.assertIn("GET /exports/{item_id}", routes)
        self.assertIn("HEAD /exports/{item_id}", routes)

    def test_discovers_environment_and_marks_documentation(self) -> None:
        result = inventory.build_inventory(self.root)
        values = {item["id"]: item for item in result["environment_variables"]}
        secret = values["WEB_AUTH_SECRET"]
        self.assertEqual(secret["sensitivity"], "secret")
        self.assertTrue(secret["documented_in_env_example"])
        self.assertEqual(secret["test_coverage"]["status"], "covered")
        self.assertEqual(values["DATABASE_PATH"]["sensitivity"], "configuration")

    def test_discovers_connector_capabilities(self) -> None:
        result = inventory.build_inventory(self.root)
        connectors = {item["id"]: item for item in result["connectors"]}
        fitbit = connectors["fitbit"]
        self.assertEqual(fitbit["class"], "FitbitConnector")
        self.assertEqual(fitbit["authentication"], "oauth")
        self.assertEqual(fitbit["metrics"], ["heart_rate", "sleep"])
        self.assertEqual(fitbit["test_coverage"]["status"], "covered")

    def test_discovers_workflow_metadata(self) -> None:
        result = inventory.build_inventory(self.root)
        workflows = {item["id"]: item for item in result["workflows"]}
        workflow = workflows[".github/workflows/ci.yml"]
        self.assertEqual(workflow["name"], "CI")
        self.assertEqual(workflow["triggers"], ["pull_request", "push"])
        self.assertTrue(workflow["uses_secrets"])
        self.assertEqual(workflow["authentication"], "github-token")

    def test_shared_views_are_derived_from_canonical_inventory(self) -> None:
        result = inventory.build_inventory(self.root)
        self.assertIn("GET /api/history", result["views"]["security"]["routes"])
        self.assertIn(
            "WEB_AUTH_SECRET", result["views"]["security"]["environment_variables"]
        )
        self.assertIn("fitbit", result["views"]["onboarding"]["connectors"])
        self.assertIn("GET /login", result["views"]["documentation"]["routes"])
        self.assertNotIn(
            "GET /api/history", result["views"]["tests"]["uncovered_routes"]
        )

    def test_validation_accepts_complete_inventory(self) -> None:
        result = inventory.build_inventory(self.root)
        self.assertEqual(inventory.validate_inventory(result), [])

    def test_output_is_deterministic_and_renderable(self) -> None:
        first = inventory.build_inventory(self.root)
        second = inventory.build_inventory(self.root)
        self.assertEqual(first, second)
        markdown = inventory.render_markdown(first)
        self.assertIn("# Repository inventory", markdown)
        self.assertIn("`/api/history`", markdown)
        encoded = json.dumps(first, sort_keys=True)
        self.assertIn("environment_variables", encoded)


if __name__ == "__main__":
    unittest.main()
