from unittest import TestCase

from cli_downloader.cli.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    TranslationService,
    current_translator,
    resolve_locale,
    translation_context,
    translate_status,
    validate_catalogs,
)
from cli_downloader.cli.shell import _display_status


class TranslationServiceTest(TestCase):
    def test_catalogs_have_the_same_required_keys(self) -> None:
        self.assertEqual(validate_catalogs(), ())

    def test_supports_requested_locales_and_translates_values(self) -> None:
        self.assertEqual(SUPPORTED_LOCALES, ("en-US", "zh-TW", "zh-CN"))
        self.assertEqual(TranslationService("en-US")("common.ready"), "Ready")
        self.assertEqual(TranslationService("zh-TW")("common.ready"), "就緒")
        self.assertEqual(TranslationService("zh-CN")("common.ready"), "就绪")

    def test_falls_back_from_system_and_unknown_locales_to_english(self) -> None:
        self.assertEqual(resolve_locale("zh_TW"), "zh-TW")
        self.assertEqual(resolve_locale("system", system_language="zh-CN"), "zh-CN")
        self.assertEqual(resolve_locale("ja-JP"), DEFAULT_LOCALE)
        self.assertEqual(TranslationService("zh-TW")("missing.key"), "missing.key")

    def test_human_statuses_follow_the_selected_locale(self) -> None:
        self.assertEqual(_display_status("queued", TranslationService("zh-TW")), "排隊中")
        self.assertEqual(_display_status("completed", TranslationService("zh-CN")), "已完成")
        self.assertEqual(translate_status("failed", TranslationService("zh-TW")), "失敗")

    def test_translation_context_is_scoped_to_one_cli_invocation(self) -> None:
        self.assertEqual(current_translator().locale, "en-US")
        with translation_context(TranslationService("zh-TW")):
            self.assertEqual(current_translator().locale, "zh-TW")
        self.assertEqual(current_translator().locale, "en-US")

    def test_language_options_keep_native_names_in_every_ui_locale(self) -> None:
        for locale in SUPPORTED_LOCALES:
            translator = TranslationService(locale)
            self.assertEqual(translator("settings.language.en-US"), "English")
            self.assertEqual(translator("settings.language.zh-TW"), "繁體中文")
            self.assertEqual(translator("settings.language.zh-CN"), "简体中文")
