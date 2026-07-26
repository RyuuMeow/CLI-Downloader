"""Application identity and links exposed to user-facing surfaces."""

from cli_downloader import __version__, project_url

PROJECT_NAME = "CLI-Downloader"
REPOSITORY_URL = project_url("Repository")

__all__ = ["PROJECT_NAME", "REPOSITORY_URL", "__version__"]
