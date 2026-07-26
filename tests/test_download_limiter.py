import asyncio
from email.message import Message
from unittest import TestCase
from urllib.error import HTTPError

from cli_downloader.application.download_limiter import GalleryDownloadLimiter, RequestPolicy


class GalleryDownloadLimiterTest(TestCase):
    def test_bounds_concurrent_galleries(self) -> None:
        async def run() -> int:
            limiter = GalleryDownloadLimiter(2)
            active = 0
            peak = 0

            async def download() -> None:
                nonlocal active, peak
                async with limiter:
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.01)
                    active -= 1

            await asyncio.gather(*(download() for _ in range(5)))
            return peak

        self.assertEqual(asyncio.run(run()), 2)

    def test_rejects_invalid_limit(self) -> None:
        with self.assertRaises(ValueError):
            GalleryDownloadLimiter(0)

    def test_item_context_uses_root_and_site_scheduler_identity(self) -> None:
        async def run() -> list[str]:
            limiter = GalleryDownloadLimiter(1, item_site_limit=1)
            order: list[str] = []

            async def item(root_id: str) -> None:
                async with limiter.item(root_id=root_id, site_id="pixiv"):
                    order.append(root_id)
                    await asyncio.sleep(0)

            await asyncio.gather(item("batch-a"), item("item-b"))
            return order

        self.assertEqual(asyncio.run(run()), ["batch-a", "item-b"])

    def test_retries_rate_limited_request_and_reports_wait(self) -> None:
        async def run() -> tuple[str, int, list[dict[str, object]]]:
            policy = RequestPolicy(
                min_interval_seconds=0,
                max_attempts=3,
                max_backoff_seconds=1,
            )
            limiter = GalleryDownloadLimiter(request_policies={"pixiv": policy})
            attempts = 0
            reports: list[dict[str, object]] = []

            async def request() -> str:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    headers = Message()
                    headers["Retry-After"] = "0"
                    raise HTTPError("https://example.test", 429, "limited", headers, None)
                return "ok"

            result = await limiter.request("pixiv", request, report_retry=reports.append)
            return result, attempts, reports

        result, attempts, reports = asyncio.run(run())

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)
        self.assertEqual([report["attempt"] for report in reports], [1, 2])
        self.assertTrue(all(report["http_status"] == 429 for report in reports))

    def test_exhausted_rate_limit_records_attempt_count(self) -> None:
        async def run() -> None:
            policy = RequestPolicy(min_interval_seconds=0, max_attempts=2)
            limiter = GalleryDownloadLimiter(request_policies={"pixiv": policy})

            async def request() -> None:
                headers = Message()
                headers["Retry-After"] = "0"
                raise HTTPError("https://example.test", 429, "limited", headers, None)

            await limiter.request("pixiv", request)

        with self.assertRaises(HTTPError) as raised:
            asyncio.run(run())

        self.assertEqual(raised.exception.request_attempt_count, 2)
        self.assertIsInstance(raised.exception.next_retry_at, str)
