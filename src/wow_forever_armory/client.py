"""Small asynchronous client for public Armory character pages."""

import asyncio
import json
import logging
import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import Character
from .parser import ArmoryParseError, parse_character

_LOGGER = logging.getLogger(__name__)
_RETRYABLE = {429, 500, 502, 503, 504}


class ArmoryClientError(RuntimeError):
    """Public Armory request failed."""


class ArmoryTimeoutError(ArmoryClientError):
    """Public Armory request timed out."""


class ArmoryHTTPError(ArmoryClientError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class JsonLogFormatter(logging.Formatter):
    """Optional structured formatter for client log records."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
                "status": getattr(record, "status", None),
                "attempt": getattr(record, "attempt", None),
            }
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except TypeError, ValueError, IndexError:
            return None
        if date.tzinfo is None:
            return None
        seconds = (date - datetime.now(UTC)).total_seconds()
    return max(seconds, 0.0) if math.isfinite(seconds) else None


class ArmoryClient:
    """Fetch public profile HTML; callers supply its path under the configured base URL.

    The blocking stdlib HTTP request runs in a worker thread so this API can be
    used from an async application without blocking its event loop.
    """

    def __init__(
        self,
        base_url: str = "https://worldofwarcraft.blizzard.com/",
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        max_retry_delay: float = 30.0,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTPS URL without credentials")
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not 0 <= max_retries <= 5
            or not math.isfinite(backoff_base)
            or backoff_base < 0
            or not math.isfinite(max_retry_delay)
            or max_retry_delay < 0
        ):
            raise ValueError("invalid timeout or retry settings")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_retry_delay = max_retry_delay

    def _url(self, path: str) -> str:
        parsed = urlparse(path)
        if (
            not path
            or path.startswith("/")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
        ):
            raise ValueError("path must be a nonempty relative URL without a fragment")
        if any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
            raise ValueError("path cannot contain dot segments")
        url = urljoin(self.base_url, path)
        base = urlparse(self.base_url)
        resolved = urlparse(url)
        if resolved.netloc != base.netloc or not resolved.path.startswith(base.path):
            raise ValueError("path escapes the configured Armory base URL")
        return url

    def _request(self, url: str) -> str:
        request = Request(
            url,
            headers={"User-Agent": "wow-forever-armory-bot/0.1", "Accept": "text/html"},
        )
        with build_opener(_NoRedirect).open(request, timeout=self.timeout) as response:
            body: bytes = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise ArmoryClientError("Armory page exceeds 5 MB limit")
            return body.decode("utf-8")

    async def _get_html(self, url: str) -> str:
        return await asyncio.wait_for(
            asyncio.to_thread(self._request, url), timeout=self.timeout + 1
        )

    async def fetch_character(self, path: str) -> Character:
        """Fetch and parse a public character path, retrying transient failures."""
        url = self._url(path)
        for attempt in range(self.max_retries + 1):
            try:
                html = await self._get_html(url)
                return parse_character(html)
            except HTTPError as exc:
                status = exc.code
                delay = _retry_after(exc.headers.get("Retry-After"))
                if status not in _RETRYABLE or attempt == self.max_retries:
                    raise ArmoryHTTPError(
                        status, f"Armory returned HTTP {status}"
                    ) from exc
                if delay is not None and delay > self.max_retry_delay:
                    raise ArmoryHTTPError(
                        status, f"Armory requested a retry after {delay:.0f}s"
                    ) from exc
                if delay is None:
                    delay = min(
                        self.backoff_base * 2**attempt + random.uniform(0, 0.1),
                        self.max_retry_delay,
                    )
                _LOGGER.warning(
                    "armory_retry", extra={"status": status, "attempt": attempt + 1}
                )
                await asyncio.sleep(delay)
            except TimeoutError as exc:
                if attempt == self.max_retries:
                    raise ArmoryTimeoutError("Armory request timed out") from exc
                _LOGGER.warning(
                    "armory_retry", extra={"status": None, "attempt": attempt + 1}
                )
                await asyncio.sleep(
                    min(self.backoff_base * 2**attempt, self.max_retry_delay)
                )
            except URLError as exc:
                raise ArmoryClientError(
                    f"Armory connection failed: {exc.reason}"
                ) from exc
            except UnicodeError as exc:
                raise ArmoryClientError("Armory response is not valid UTF-8") from exc
            except ArmoryParseError:
                raise
        raise AssertionError("retry loop exhausted")
