"""Copy this layout, then replace placeholders using current repository contracts."""

from __future__ import annotations


class Plugin:
    site_id = "example"
    display_name = "Example"

    def get_manifest(self):
        raise NotImplementedError("Load the validated manifest using the current plugin pattern")

    def can_handle_url(self, url: str) -> bool:
        return self.classify_url(url) is not None

    def classify_url(self, url: str):
        raise NotImplementedError("Return the current core RouteMatch contract")

    async def execute(self, request):
        raise NotImplementedError("Return the current core OperationResult contract")

    async def resolve(self, request):
        raise NotImplementedError("Return the current core ResolvedItem contract")

    async def list_assets(self, item):
        raise NotImplementedError("Return the current core AssetList contract")

    async def choose_strategy(self, item):
        raise NotImplementedError("Return the current core DownloadStrategyPlan contract")
