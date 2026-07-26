"""Private frozen entry point for version-scoped plugin dependencies."""

from __future__ import annotations


def main() -> int:
    from pip._internal.cli.main import main as pip_main

    return pip_main()


if __name__ == "__main__":
    raise SystemExit(main())
