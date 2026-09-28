"""Offline tests for actionable AI setup status; no real keys or API calls."""
import unittest
from unittest.mock import patch

from adaptive_crypto_dashboard import ai_configuration


class AIConfigurationTests(unittest.TestCase):
    def configuration(self, values):
        return ai_configuration(values)

    def test_reports_all_missing_setup(self):
        result = self.configuration({})
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "off")
        self.assertEqual(result["missing"], ["API key", "model"])
        self.assertIsNone(result["model"])

    def test_direct_http_requires_no_optional_sdk(self):
        result = self.configuration({"OPENAI_API_KEY": "fake-key", "OPENAI_MODEL": "test-model"})
        self.assertTrue(result["ready"])
        self.assertEqual(result["missing"], [])

    def test_missing_model_is_reported_separately(self):
        result = self.configuration({"OPENAI_API_KEY": "fake-key"})
        self.assertEqual(result["missing"], ["model"])

    def test_missing_key_is_reported_separately(self):
        result = self.configuration({"OPENAI_MODEL": "test-model"})
        self.assertEqual(result["missing"], ["API key"])

    def test_whitespace_only_values_are_not_configuration(self):
        result = self.configuration({"OPENAI_API_KEY": " \n ", "OPENAI_MODEL": "\t "})
        self.assertFalse(result["ready"])
        self.assertEqual(result["missing"], ["API key", "model"])

    def test_ready_status_keeps_key_private_and_trims_model(self):
        result = self.configuration({"OPENAI_API_KEY": "private-test-value", "OPENAI_MODEL": " test-model "})
        self.assertEqual(result, {"ready": True, "status": "ready", "missing": [], "model": "test-model", "provider": "openai"})
        self.assertNotIn("private-test-value", repr(result))

    def test_unknown_provider_never_falls_back_to_openai(self):
        result = ai_configuration({"AI_PROVIDER": "unknown", "OPENAI_API_KEY": "fake-key", "OPENAI_MODEL": "test-model"})
        self.assertFalse(result["ready"])
        self.assertIn("supported provider", result["missing"])

    def test_gemini_uses_only_its_own_key(self):
        values = {"AI_PROVIDER": "gemini", "OPENAI_API_KEY": "wrong-provider-key", "GEMINI_MODEL": "test-gemini"}
        self.assertEqual(ai_configuration(values)["missing"], ["API key"])
        values["GEMINI_API_KEY"] = "private-gemini-key"
        self.assertTrue(ai_configuration(values)["ready"])
        self.assertNotIn("private-gemini-key", repr(ai_configuration(values)))

    def test_process_environment_is_supported(self):
        with patch.dict("adaptive_crypto.notifications.os.environ", {"OPENAI_API_KEY": "fake-key", "OPENAI_MODEL": "test-model"}, clear=True):
            self.assertTrue(ai_configuration()["ready"])


if __name__ == "__main__":
    unittest.main()
