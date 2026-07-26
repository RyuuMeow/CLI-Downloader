from unittest import TestCase

from cli_downloader.config.schema import default_schema
from cli_downloader.core.errors import ConfigValidationError


class ConfigSchemaTest(TestCase):
    def test_default_schema_has_expected_defaults(self) -> None:
        defaults = default_schema().defaults()

        self.assertEqual(defaults["interface.language"], "en-US")
        self.assertEqual(defaults["download.strategy"], "auto")
        self.assertEqual(defaults["download.folder_template"], "{site_id}/{gallery_id}-{title}")
        self.assertEqual(defaults["download.max_concurrent_galleries"], 2)
        self.assertEqual(defaults["scheduler.policy"], "round_robin")
        self.assertEqual(defaults["scheduler.max_concurrent_downloads"], 2)
        self.assertEqual(defaults["scheduler.max_concurrent_downloads_per_site"], 2)
        self.assertEqual(defaults["network.max_attempts"], 5)
        self.assertEqual(defaults["network.max_backoff_seconds"], 300.0)
        self.assertEqual(defaults["profile.default"], "default")

    def test_default_schema_rejects_unknown_strategy(self) -> None:
        with self.assertRaises(ConfigValidationError):
            default_schema().validate({"download.strategy": "filter"})

    def test_default_schema_rejects_unknown_interface_language(self) -> None:
        with self.assertRaises(ConfigValidationError):
            default_schema().validate({"interface.language": "ja-JP"})

    def test_default_schema_rejects_empty_folder_template(self) -> None:
        with self.assertRaises(ConfigValidationError):
            default_schema().validate({"download.folder_template": ""})

    def test_default_schema_rejects_non_positive_gallery_concurrency(self) -> None:
        with self.assertRaises(ConfigValidationError):
            default_schema().validate({"download.max_concurrent_galleries": 0})

    def test_scheduler_policy_and_limits_are_validated(self) -> None:
        with self.assertRaises(ConfigValidationError):
            default_schema().validate({"scheduler.policy": "weighted"})
        with self.assertRaises(ConfigValidationError):
            default_schema().validate({"scheduler.max_concurrent_downloads": 0})
