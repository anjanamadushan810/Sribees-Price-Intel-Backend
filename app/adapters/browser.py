"""Render public pages in a real browser engine, the way search engines do, for shops
whose prices only appear after the page's own JavaScript runs (Keells).

Same politeness as PoliteClient: robots.txt is checked by the adapter before any
page, one page per `delay` seconds, our bot name in the User-Agent, and any sign of
a block (403/429, a captcha or challenge) ends the run. We never solve a captcha.
"""

import asyncio
import time

from app.adapters.base import SourceBlocked, SourceFailed
from app.core.config import get_settings

_CHALLENGE_TEXT = ("captcha", "verify you are human", "access denied", "unusual traffic")


class Renderer:
    def __init__(self, delay: float):
        self.delay = max(delay, get_settings().scrape_delay_seconds)
        self.pages = 0
        self._last = 0.0
        self._pw = self._browser = self._context = None

    async def __aenter__(self) -> "Renderer":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch()
        self._context = await self._browser.new_context(
            user_agent=f"Mozilla/5.0 (compatible; {get_settings().scrape_user_agent})",
            locale="en-GB",
        )
        # Pictures, fonts and media are not needed to read a price.
        await self._context.route(
            "**/*", lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font") else route.continue_())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def render(self, url: str, ready_selector: str, timeout_ms: int = 30_000):
        """Open `url`, wait for `ready_selector`, return the page (caller closes it)."""
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()
        self.pages += 1
        page = await self._context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if resp is not None and resp.status in (401, 403, 429):
                raise SourceBlocked(f"HTTP {resp.status} on {url}")
            try:
                await page.wait_for_selector(ready_selector, timeout=timeout_ms)
            except Exception as exc:
                body = (await page.inner_text("body")).lower()
                if any(t in body for t in _CHALLENGE_TEXT):
                    raise SourceBlocked(f"challenge page on {url}") from exc
                return page  # loaded, but this product shows no price (gone / not sold)
            return page
        except SourceBlocked:
            await page.close()
            raise
        except Exception as exc:
            await page.close()
            raise SourceFailed(f"could not render {url}: {exc.__class__.__name__}") from exc
