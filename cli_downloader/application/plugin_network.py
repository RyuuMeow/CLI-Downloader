"""Compatibility boundary for plugins not yet using Core's HTTP client."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from cli_downloader.application.site_request_coordinator import (
    RequestContext,
    RequestGate,
    request_context,
)
from cli_downloader.core.http import has_managed_http


async def invoke_plugin_network(instance, limiter, site_id, operation, *, context: RequestContext):
    with request_context(context):
        if limiter is None:
            return await operation()
        if has_managed_http(instance):
            if context.checkpoint is None:
                return await operation()
            return await _controlled_operation(limiter.requests, site_id, operation, context)
        # A whole plugin operation can have side effects and internal retries;
        # only the plugin's managed transport may retry individual HTTP requests.
        return await limiter.request(site_id, operation, retry_allowed=False)


async def checkpoint_control(control) -> None:
    wait = getattr(control, "wait_if_paused", None)
    if callable(wait):
        await wait()
    if control.is_set():
        raise asyncio.CancelledError


async def _controlled_operation(coordinator, site_id, operation, context):
    # The supervisor alone acknowledges pause. Child requests cannot claim the
    # whole operation is paused while another child still owns a live response.
    gate = RequestGate()
    with request_context(replace(context, gate=gate)):
        pending = asyncio.create_task(operation())
    released_item = False
    try:
        while not pending.done():
            gate.blocked = True
            if coordinator.gate_idle(gate):
                cooling = coordinator.cooling(site_id)
                if cooling and not released_item and context.release_item is not None:
                    await context.release_item()
                    released_item = True
                await context.checkpoint()
                if not cooling and released_item and context.resume_item is not None:
                    await context.resume_item()
                    released_item = False
            gate.blocked = False
            coordinator.wake()
            await asyncio.wait({pending}, timeout=0.05)
        return await pending
    finally:
        gate.blocked = False
        coordinator.wake()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
