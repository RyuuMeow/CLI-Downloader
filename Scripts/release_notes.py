"""Validate and extract the curated notes for one tagged release."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


_HEADING_RE = re.compile(
    r"^## (?P<title>v(?P<version>[^ ]+) - (?P<date>\d{4}-\d{2}-\d{2}))\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ReleaseNotes:
    version: str
    title: str
    date: str
    body: str


def extract_release_notes(text: str, version: str) -> ReleaseNotes:
    """Extract one exact version entry and reject ambiguous or incomplete notes."""

    normalized_version = version.removeprefix("v")
    if not normalized_version:
        raise ValueError("release version must not be empty")

    matches = [match for match in _HEADING_RE.finditer(text) if match.group("version") == normalized_version]
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"missing changelog entry for v{normalized_version}")
        raise ValueError(f"duplicate changelog entries for v{normalized_version}")

    match = matches[0]
    release_date = match.group("date")
    try:
        date.fromisoformat(release_date)
    except ValueError as error:
        raise ValueError(f"invalid release date for v{normalized_version}: {release_date}") from error

    next_heading = re.search(r"^## ", text[match.end() :], flags=re.MULTILINE)
    entry_end = match.end() + next_heading.start() if next_heading else len(text)
    entry = text[match.end() : entry_end]
    separator = re.search(r"^\s*---\s*$", entry, flags=re.MULTILINE)
    if separator is None:
        raise ValueError(f"changelog entry for v{normalized_version} must end with ---")
    body = entry[: separator.start()].replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ValueError(f"changelog entry for v{normalized_version} has an empty body")

    return ReleaseNotes(
        version=normalized_version,
        title=match.group("title"),
        date=release_date,
        body=body,
    )


def validate_release_metadata(
    *,
    changelog: Path,
    project_file: Path,
    version: str,
    tag: str | None = None,
) -> ReleaseNotes:
    """Validate tag/package/changelog agreement before packaging starts."""

    normalized_version = version.removeprefix("v")
    if tag is not None and tag != f"v{normalized_version}":
        raise ValueError(f"release tag {tag!r} does not match v{normalized_version}")
    with project_file.open("rb") as handle:
        project_version = str(tomllib.load(handle)["project"]["version"])
    if project_version != normalized_version:
        raise ValueError(
            f"project version {project_version!r} does not match release version {normalized_version!r}"
        )
    return extract_release_notes(changelog.read_text(encoding="utf-8"), normalized_version)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--title-file", type=Path)
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--json", action="store_true", help="Print extracted metadata as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        notes = validate_release_metadata(
            changelog=args.changelog,
            project_file=args.project_file,
            version=args.version,
            tag=args.tag,
        )
    except (OSError, KeyError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"release metadata error: {error}", file=sys.stderr)
        return 1

    if args.title_file:
        args.title_file.parent.mkdir(parents=True, exist_ok=True)
        args.title_file.write_text(notes.title + "\n", encoding="utf-8")
    if args.body_file:
        args.body_file.parent.mkdir(parents=True, exist_ok=True)
        args.body_file.write_text(notes.body + "\n", encoding="utf-8")
    if args.json or not args.title_file and not args.body_file:
        print(json.dumps(asdict(notes), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
