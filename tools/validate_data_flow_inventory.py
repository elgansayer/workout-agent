#!/usr/bin/env python3
"""Validate the privacy notice and machine-readable data-flow inventory.

The key drift check is intentionally derived from code registries rather than a
second hand-maintained list: newly registered AI or health providers must be
added to docs/data-flow-inventory.json in the same change.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "docs" / "data-flow-inventory.json"

REQUIRED_NOTICE_SECTIONS = (
    "## What we collect",
    "## Why we use the data",
    "## Who receives data",
    "## Retention and deletion",
    "## Your choices and rights",
    "## Contact",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_ai_provider_keys(root: Path) -> set[str]:
    tree = ast.parse((root / "backend" / "ai_provider.py").read_text(encoding="utf-8"))
    for node in tree.body:
        target_name = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            target_name = names[0] if names else None
            value = node.value
        if target_name == "PROVIDERS" and isinstance(value, ast.Dict):
            return {
                key.value
                for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise ValueError("Could not locate ai_provider.PROVIDERS registry")


def _connector_key(class_name: str) -> str:
    if class_name.startswith("Garmin"):
        return "garmin"
    return re.sub(r"Connector$", "", class_name).lower()


def _extract_health_connector_keys(root: Path) -> set[str]:
    tree = ast.parse(
        (root / "backend" / "connectors" / "builtin.py").read_text(encoding="utf-8")
    )
    classes = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("Connector")
    }
    return {_connector_key(name) for name in classes}


def discover_integrations(root: Path = ROOT) -> set[str]:
    integrations = _extract_ai_provider_keys(root) | _extract_health_connector_keys(root)

    if (root / "backend" / "hevy_reader.py").exists():
        integrations.add("hevy")
    if (root / "backend" / "google_health_auth.py").exists():
        integrations.add("google_health")

    for notifier in (root / "backend").glob("*_notifier.py"):
        integrations.add(notifier.stem.removesuffix("_notifier"))

    database = (root / "backend" / "database.py").read_text(encoding="utf-8")
    webapp = (root / "backend" / "webapp" / "app.py").read_text(encoding="utf-8")
    if "save_push_subscription" in database or "save_push_subscription" in webapp:
        integrations.add("web_push")

    return integrations


def validate_privacy_notice_text(text: str) -> list[str]:
    errors: list[str] = []
    for section in REQUIRED_NOTICE_SECTIONS:
        if section not in text:
            errors.append(f"privacy notice missing section: {section}")
    if "data-flow-inventory.json" not in text:
        errors.append("privacy notice does not link the machine-readable inventory")
    if "Do **not** publish" not in text and "Do not publish" not in text:
        errors.append("privacy notice must warn users not to post personal data publicly")
    return errors


def validate_inventory(
    inventory: dict[str, Any],
    *,
    root: Path = ROOT,
    notice_text: str | None = None,
) -> list[str]:
    errors: list[str] = []

    if inventory.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    version = str(inventory.get("inventory_version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append("inventory_version must be semantic version X.Y.Z")

    data_classes = set((inventory.get("data_classes") or {}).keys())
    processors = inventory.get("processors") or []
    processor_ids = {item.get("id") for item in processors if isinstance(item, dict)}
    systems = inventory.get("systems") or []
    system_ids = [item.get("id") for item in systems if isinstance(item, dict)]
    if len(system_ids) != len(set(system_ids)):
        errors.append("system IDs must be unique")
    system_id_set = set(system_ids)

    for required_system in ("browser", "fastapi", "sqlite"):
        if required_system not in system_id_set:
            errors.append(f"required core system is missing: {required_system}")

    declared_integrations: set[str] = set()
    for system in systems:
        if not isinstance(system, dict):
            errors.append("every system entry must be an object")
            continue
        sid = system.get("id", "<unknown>")
        for field in ("name", "category", "processor", "data_classes", "purposes", "retention", "code_refs"):
            if not system.get(field):
                errors.append(f"system {sid} missing {field}")
        if system.get("processor") not in processor_ids:
            errors.append(f"system {sid} references unknown processor {system.get('processor')}")
        unknown_classes = set(system.get("data_classes") or []) - data_classes
        if unknown_classes:
            errors.append(f"system {sid} uses unknown data classes: {sorted(unknown_classes)}")
        if system.get("integration_key"):
            declared_integrations.add(str(system["integration_key"]))

    discovered = discover_integrations(root)
    missing = discovered - declared_integrations
    if missing:
        errors.append(
            "code integrations missing from data-flow inventory: " + ", ".join(sorted(missing))
        )

    flows = inventory.get("data_flows") or []
    flow_ids: list[str] = []
    for flow in flows:
        if not isinstance(flow, dict):
            errors.append("every data flow must be an object")
            continue
        fid = str(flow.get("id", "<unknown>"))
        flow_ids.append(fid)
        if flow.get("source") not in system_id_set:
            errors.append(f"flow {fid} has unknown source {flow.get('source')}")
        if flow.get("destination") not in system_id_set:
            errors.append(f"flow {fid} has unknown destination {flow.get('destination')}")
        unknown_classes = set(flow.get("data_classes") or []) - data_classes
        if unknown_classes:
            errors.append(f"flow {fid} uses unknown data classes: {sorted(unknown_classes)}")
        if not flow.get("purpose"):
            errors.append(f"flow {fid} missing purpose")
    if len(flow_ids) != len(set(flow_ids)):
        errors.append("flow IDs must be unique")

    notice_path = root / str(inventory.get("privacy_notice", "docs/PRIVACY.md"))
    if notice_text is None:
        if not notice_path.exists():
            errors.append(f"privacy notice does not exist: {notice_path.relative_to(root)}")
        else:
            notice_text = notice_path.read_text(encoding="utf-8")
    if notice_text is not None:
        errors.extend(validate_privacy_notice_text(notice_text))
        if version and f"Notice version:** {version}" not in notice_text:
            errors.append("privacy notice version does not match inventory_version")

    return errors


def main() -> int:
    inventory = _load_json(INVENTORY_PATH)
    errors = validate_inventory(inventory)
    if errors:
        print("Data-flow inventory validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    discovered = sorted(discover_integrations())
    print(
        f"Validated inventory {inventory['inventory_version']} with "
        f"{len(inventory['systems'])} systems, {len(inventory['data_flows'])} flows "
        f"and {len(discovered)} code-discovered integrations."
    )
    print("Discovered integrations: " + ", ".join(discovered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
