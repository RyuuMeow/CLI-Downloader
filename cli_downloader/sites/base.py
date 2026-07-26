from __future__ import annotations

from typing import Protocol

from cli_downloader.core.operations import OperationRequest, OperationResult, RouteMatch
from cli_downloader.core.models import (
    AssetList,
    DownloadStrategyPlan,
    ResolveRequest,
    ResolvedItem,
)


class SiteAdapter(Protocol):
    site_id: str
    display_name: str

    def can_handle_url(self, url: str) -> bool: ...

    async def resolve(self, request: ResolveRequest) -> ResolvedItem: ...

    async def list_assets(self, item: ResolvedItem) -> AssetList: ...

    async def choose_strategy(self, item: ResolvedItem) -> DownloadStrategyPlan: ...


class OperationPlugin(Protocol):
    site_id: str
    display_name: str

    def classify_url(self, url: str) -> RouteMatch | None: ...

    async def execute(self, request: OperationRequest) -> OperationResult: ...
