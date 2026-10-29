"""What every source adapter shares: a polite HTTP client and the item shape.

Politeness rules (docs/PHASE0_FINDINGS.md): robots.txt is obeyed, one request per
host every scrape_delay_seconds, and a block (403/429/bot challenge) ends the run at
once. We never retry around a block and never work around bot protection.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.robotparser import RobotFileParser

import httpx

from app.adapters import botauth
from app.core.config import get_settings


class SourceBlocked(Exception):
    """The site said no (robots.txt, 403, 429, a challenge page). Stop, do not retry."""


class SourceFailed(Exception):
    """The site broke (5xx, timeouts, a page we cannot read). Retry on the next run."""


@dataclass(frozen=True)
class ListedItem:
    external_id: str
    name: str
    price: Decimal
    mrp: Decimal | None = None
    brand: str | None = None
    pack: str | None = None
    barcode: str | None = None
    url: str | None = None
    in_stock: bool = True
    image: str | None = None  # absolute URL of the shop's product photo


def absolute(base_url: str, src: str | None) -> str | None:
    """A photo address as the browser needs it: '//cdn/x', '/uploads/x' and
    '../src/x' all become https URLs on the shop's own site."""
    if not src:
        return None
    src = src.strip()
    if src.startswith(("http://", "https://")):
        return src[:500]
    if src.startswith("//"):
        return f"https:{src}"[:500]
    while src.startswith("../"):
        src = src[3:]
    return f"{base_url.rstrip('/')}/{src.lstrip('/')}"[:500]


def money(value) -> Decimal | None:
    """'₨ 1,250.00', '1250', 1250.0 -> Decimal('1250.00'); anything unreadable or <= 0 -> None."""
    if value is None:
        return None
    text = "".join(c for c in str(value) if c.isdigit() or c == ".")
    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


_CHALLENGE_MARKERS = ("__zenedge", "cf-chl-", "challenge-platform", "captcha")


class PoliteClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        settings = get_settings()
        self.base_url = base_url.rstrip("/")
        self.delay = settings.scrape_delay_seconds
        self.user_agent = settings.scrape_user_agent
        self.requests = 0
        self._last = 0.0
        self._robots: RobotFileParser | None = None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"User-Agent": self.user_agent, "Accept-Language": "en"},
            timeout=settings.scrape_timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _pace(self) -> None:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    async def _allowed(self, path: str) -> bool:
        if self._robots is None:
            resp = None
            for attempt in range(3):
                await self._pace()
                self.requests += 1
                try:
                    resp = await self._client.get("/robots.txt")
                    break
                except httpx.TransportError as exc:
                    if attempt == 2:
                        raise SourceFailed(
                            f"robots.txt unreachable: {exc.__class__.__name__}") from exc
                    await asyncio.sleep(5 * (attempt + 1))
            robots = RobotFileParser()
            if resp.status_code == 200 and not _looks_challenged(resp.text):
                robots.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403) or _looks_challenged(resp.text):
                raise SourceBlocked("robots.txt is behind a block or a bot challenge")
            else:
                robots.parse([])  # no robots.txt: nothing is disallowed
            # Kept only once read: a failed read must never leave "allow everything" behind.
            self._robots = robots
        return self._robots.can_fetch(self.user_agent, f"{self.base_url}{path}")

    async def allowed(self, path: str) -> bool:
        """robots.txt says we may fetch `path` (also used before rendering a page)."""
        return await self._allowed(path)

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        """A read-only form post, the way a shop's own search box sends it."""
        return await self._request("POST", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not await self._allowed(path):
            raise SourceBlocked(f"robots.txt disallows {path}")
        waited = False
        for attempt in range(3):
            await self._pace()
            self.requests += 1
            try:
                resp = await self._client.request(
                    method, path,
                    headers=botauth.request_headers(self._client.base_url.netloc.decode()),
                    **kwargs)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SourceFailed(f"network error: {exc.__class__.__name__}") from exc
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429 and not waited:
                # A short, stated back-off is honoured once; anything else is a block.
                retry_after = _retry_after(resp)
                if retry_after is not None and retry_after <= MAX_RETRY_AFTER:
                    waited = True
                    await asyncio.sleep(retry_after)
                    continue
            if resp.status_code in (401, 403, 429):
                raise SourceBlocked(f"HTTP {resp.status_code} on {path}")
            if resp.status_code >= 500:
                if attempt == 2:
                    raise SourceFailed(f"HTTP {resp.status_code} on {path}")
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise SourceFailed(f"HTTP {resp.status_code} on {path}")
            if "html" in resp.headers.get("content-type", "") and _looks_challenged(resp.text):
                raise SourceBlocked(f"bot challenge on {path}")
            return resp
        raise SourceFailed("unreachable")  # pragma: no cover


MAX_RETRY_AFTER = 120


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After header; None when absent or a date."""
    try:
        return max(0.0, float(resp.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def _looks_challenged(text: str) -> bool:
    """A challenge page is a near-empty page carrying a bot-check marker. Real pages may
    load the same vendor script (Glomark serves Zenedge's script on every page)."""
    head = text[:4000].lower()
    if "just a moment" in head:
        return True
    return len(text) < 6000 and any(marker in head for marker in _CHALLENGE_MARKERS)


class Adapter:
    """mode 'catalogue': read the whole shop (cheap JSON sources).
    mode 'search': look up one product name at a time (HTML sources)."""

    mode = "catalogue"
    # Some shops ask for a slower pace than our default.
    min_delay = 0.0

    def __init__(self, client: PoliteClient):
        self.client = client
        client.delay = max(client.delay, self.min_delay)

    async def __aenter__(self) -> "Adapter":
        """Adapters that need a browser start it here; the run wraps them in `async with`."""
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def catalogue(self) -> AsyncIterator[ListedItem]:  # pragma: no cover - interface
        raise NotImplementedError

    async def search(self, query: str) -> list[ListedItem]:  # pragma: no cover - interface
        raise NotImplementedError
