from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Scripts.release_notes import extract_release_notes, validate_release_metadata


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_exact_entry_and_preserves_unicode(self) -> None:
        text = (
            "# Changelog\r\n\r\n"
            "## v0.1.3 - 2026-07-15\r\n\r\n"
            "### Fixed\r\n- 修正下載流程\r\n\r\n---\r\n\r\n"
            "## v0.1.2 - 2026-07-10\r\n\r\n### Fixed\r\n- Older fix\r\n\r\n---\r\n"
        )

        notes = extract_release_notes(text, "v0.1.3")

        self.assertEqual(notes.title, "v0.1.3 - 2026-07-15")
        self.assertEqual(notes.body, "### Fixed\n- 修正下載流程")

    def test_rejects_duplicate_or_unterminated_entry(self) -> None:
        duplicate = "## v0.1.3 - 2026-07-15\n- one\n---\n## v0.1.3 - 2026-07-15\n- two\n---\n"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            extract_release_notes(duplicate, "0.1.3")

        with self.assertRaisesRegex(ValueError, "must end"):
            extract_release_notes("## v0.1.3 - 2026-07-15\n- missing separator\n", "0.1.3")

    def test_metadata_requires_project_and_tag_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changelog = root / "CHANGELOG.md"
            project = root / "pyproject.toml"
            changelog.write_text("## v0.1.3 - 2026-07-15\n- release\n---\n", encoding="utf-8")
            project.write_text('[project]\nversion = "0.1.3"\n', encoding="utf-8")

            notes = validate_release_metadata(
                changelog=changelog,
                project_file=project,
                version="0.1.3",
                tag="v0.1.3",
            )
            self.assertEqual(notes.version, "0.1.3")
            with self.assertRaisesRegex(ValueError, "does not match"):
                validate_release_metadata(
                    changelog=changelog,
                    project_file=project,
                    version="0.1.3",
                    tag="v0.1.4",
                )


class ReleaseWorkflowTests(unittest.TestCase):
    def test_workflow_publishes_curated_notes_and_expected_assets(self) -> None:
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("Scripts/release_notes.py", text)
        self.assertIn("--notes-file", text)
        self.assertNotIn("--generate-notes", text)
        self.assertIn("windows-x64-portable.zip", text)
        self.assertIn("windows-x64-setup.exe", text)
        self.assertIn("windows-x64.sha256", text)
        self.assertIn("--verify-tag", text)


if __name__ == "__main__":
    unittest.main()
