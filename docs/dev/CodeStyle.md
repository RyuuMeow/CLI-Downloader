# Code Style

Updated: 2026-07-05

## Current Status

Ruff 0.15.20 is the formatter and linter. The project targets Python 3.11,
uses a 100-character line length, and enables the conservative `E4`, `E7`,
`E9`, and `F` lint families.

Project code must:

- follow the existing code style in the surrounding module;
- target Python 3.11 or newer;
- use type annotations for public contracts and non-obvious values;
- prefer standard-library features unless a dependency is approved;
- keep modules focused on one responsibility;
- use succinct comments only for logic that is not self-explanatory;
- add or update unit tests for behavior changes;
- preserve ASCII unless a file or user-facing content requires Unicode.

## Naming

- Modules and functions: `snake_case`.
- Classes and protocols: `PascalCase`.
- Constants: `UPPER_SNAKE_CASE`.
- Private helpers: leading underscore.
- Stable identifiers in manifests: lowercase letters, numbers, and underscores.

Ruff configuration in `pyproject.toml` is authoritative for mechanical format
and lint behavior.

## File And Folder Conventions

- Application package: `cli_downloader/`.
- Unit tests: `tests/test_*.py`.
- Site implementations: `cli_downloader/sites/<site_id>/`.
- External plugin helpers: `cli_downloader/plugins/`.
- Documentation: `docs/`.
- Architecture decisions: one file per decision under `docs/structure/ADR/`.

## Documentation

- Documentation is the source of truth.
- Keep current state in one authoritative document.
- `Plan.md` contains pending work only.
- `Progress.md` contains completed work only.
- `TODO.md` contains unresolved decisions only.
- Architecture rationale belongs in ADR records.

## Commits

- Use Conventional Commit messages.
- Each commit represents one logical change.
- Complete tasks are committed after implementation, tests, documentation
  updates, and consistency checks.

Examples:

```text
feat: add plugin loader
fix: reject duplicate site adapters
docs: align development workflow
```

## Branches

- Do not perform non-trivial development directly on `main` or `master`.
- Use one focused branch per primary objective.
- Codex-created branches use the `codex/` prefix.
- Experimental work belongs on a dedicated research or spike branch.

## Verification

Run the complete local quality gate:

```powershell
.\Scripts\Verify.ps1
```

The script initializes the project `.venv` when needed, installs development
tools there, then checks Ruff formatting and lint, unit tests, Python
compilation, working-tree and staged diffs, and wheel construction. It stops at
the first failed gate, prints only a compact summary during normal runs, and
cleans only verified `.tmp-wheel` and `build` directories under the repository
root.

Apply formatting separately when needed:

```powershell
python -m ruff format .
```
