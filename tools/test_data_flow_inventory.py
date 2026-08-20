from __future__ import annotations

import copy
import json
import unittest

from tools.validate_data_flow_inventory import (
    INVENTORY_PATH,
    REQUIRED_NOTICE_SECTIONS,
    ROOT,
    discover_integrations,
    validate_inventory,
    validate_privacy_notice_text,
)


class DataFlowInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        cls.notice = (ROOT / "docs" / "PRIVACY.md").read_text(encoding="utf-8")

    def test_repository_inventory_is_valid(self) -> None:
        self.assertEqual([], validate_inventory(self.inventory, notice_text=self.notice))

    def test_every_discovered_integration_is_declared(self) -> None:
        declared = {
            system["integration_key"]
            for system in self.inventory["systems"]
            if system.get("integration_key")
        }
        self.assertTrue(discover_integrations().issubset(declared))

    def test_missing_provider_is_detected(self) -> None:
        mutated = copy.deepcopy(self.inventory)
        mutated["systems"] = [
            system for system in mutated["systems"] if system.get("integration_key") != "openai"
        ]
        errors = validate_inventory(mutated, notice_text=self.notice)
        self.assertTrue(any("openai" in error for error in errors))

    def test_broken_flow_reference_is_detected(self) -> None:
        mutated = copy.deepcopy(self.inventory)
        mutated["data_flows"][0]["destination"] = "missing-system"
        errors = validate_inventory(mutated, notice_text=self.notice)
        self.assertTrue(any("unknown destination" in error for error in errors))

    def test_notice_has_all_required_plain_language_sections(self) -> None:
        for section in REQUIRED_NOTICE_SECTIONS:
            self.assertIn(section, self.notice)
        self.assertEqual([], validate_privacy_notice_text(self.notice))

    def test_notice_version_matches_inventory(self) -> None:
        self.assertIn(
            f"Notice version:** {self.inventory['inventory_version']}",
            self.notice,
        )


if __name__ == "__main__":
    unittest.main()
