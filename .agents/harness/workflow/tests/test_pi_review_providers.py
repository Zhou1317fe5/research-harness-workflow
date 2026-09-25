#!/usr/bin/env python3
"""``pi_review_providers`` 的纯逻辑回归（不调用 pi、不触碰 agent 目录）。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".agents/harness/pi_review_providers.py"

TABLE = """provider   model        context  max-out  thinking  images
xiaojimao  gpt-6-astra  272K     128K     yes       yes
xiaojimao  cheap-model  128K     32.8K    no        no
openai-codex  gpt-6-astra  272K  128K     yes       yes
"""


def _load():
    spec = importlib.util.spec_from_file_location("pi_review_providers", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PiReviewProvidersTests(unittest.TestCase):
    def setUp(self):
        self.module = _load()
        self.temp = tempfile.TemporaryDirectory(prefix="pi-review-providers-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_parse_size_understands_display_suffixes(self):
        cases = {"128K": 128_000, "32.8K": 32_800, "1M": 1_000_000, "8K": 8_000, "512": 512}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.module.parse_size(text), expected)
        with self.assertRaises(self.module.ProviderSyncError):
            self.module.parse_size("many")

    def test_parse_models_table_reads_header_and_rows(self):
        rows = self.module.parse_models_table(TABLE)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["provider"], "xiaojimao")
        self.assertEqual(rows[0]["model"], "gpt-6-astra")
        self.assertEqual(rows[0]["images"], "yes")
        with self.assertRaises(self.module.ProviderSyncError):
            self.module.parse_models_table("no table here\n")

    def test_extension_providers_are_the_ones_that_disappear(self):
        enabled = self.module.parse_models_table(TABLE)
        disabled = [row for row in enabled if row["provider"] != "xiaojimao"]
        self.assertEqual(
            self.module.providers_without_extensions(enabled, disabled), ["xiaojimao"]
        )

    def test_extension_base_urls_come_from_settings_files(self):
        settings = self.root / "extension-settings"
        settings.mkdir()
        (settings / "provider-newapi.json").write_text(json.dumps({
            "providers": {"xiaojimao": {"baseUrl": "https://api.example.com", "modelApiOverrides": {}}},
            "settings": {"onboardingWarnCountdown": 0},
        }), encoding="utf-8")
        (settings / "broken.json").write_text("{not json", encoding="utf-8")
        (settings / "unrelated.json").write_text(json.dumps({"providers": "nope"}), encoding="utf-8")
        self.assertEqual(
            self.module.read_extension_base_urls(settings),
            {"xiaojimao": "https://api.example.com"},
        )
        self.assertEqual(self.module.read_extension_base_urls(self.root / "absent"), {})

    def test_build_entry_maps_capabilities_and_never_invents_credentials(self):
        rows = self.module.parse_models_table(TABLE)
        entry = self.module.build_provider_entry(
            "xiaojimao", rows, "https://api.example.com", "XIAOJIMAO_API_KEY"
        )
        self.assertEqual(entry["baseUrl"], "https://api.example.com")
        self.assertEqual(entry["apiKey"], "$XIAOJIMAO_API_KEY")
        self.assertEqual(entry["api"], "openai-completions")
        first, second = entry["models"]
        self.assertEqual(first["id"], "gpt-6-astra")
        self.assertEqual(first["contextWindow"], 272_000)
        self.assertEqual(first["maxTokens"], 128_000)
        self.assertTrue(first["reasoning"])
        self.assertEqual(first["input"], ["text", "image"])
        self.assertFalse(second["reasoning"])
        self.assertEqual(second["input"], ["text"])

        without_key = self.module.build_provider_entry("xiaojimao", rows, "https://x", None)
        self.assertNotIn("apiKey", without_key)

    def test_build_entry_rejects_a_provider_without_models(self):
        rows = self.module.parse_models_table(TABLE)
        with self.assertRaisesRegex(self.module.ProviderSyncError, "没有任何模型"):
            self.module.build_provider_entry("absent", rows, "https://x", None)

    def test_merge_preserves_other_providers_and_reports_changes(self):
        existing = {"providers": {"newapi": {"baseUrl": "https://keep"}}}
        fresh = {"xiaojimao": {"baseUrl": "https://api.example.com", "models": [{"id": "m"}]}}
        merged, changes = self.module.merge_providers(existing, fresh)
        self.assertEqual(merged["providers"]["newapi"], {"baseUrl": "https://keep"})
        self.assertEqual(merged["providers"]["xiaojimao"], fresh["xiaojimao"])
        self.assertEqual(changes, ["xiaojimao: 新增"])
        # 原对象不被就地修改
        self.assertNotIn("xiaojimao", existing["providers"])

        merged_again, changes_again = self.module.merge_providers(merged, fresh)
        self.assertEqual(changes_again, ["xiaojimao: 已是最新"])
        self.assertEqual(merged_again["providers"]["xiaojimao"], fresh["xiaojimao"])

    def test_load_existing_rejects_broken_json(self):
        path = self.root / "models.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaisesRegex(self.module.ProviderSyncError, "不是合法 JSON"):
            self.module.load_existing(path)
        self.assertEqual(self.module.load_existing(self.root / "absent.json"), {})


if __name__ == "__main__":
    unittest.main()
