from __future__ import annotations

import logging
import random
import time
import httpx


LOGGER = logging.getLogger(__name__)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        min_interval: float = 0.3,
        max_interval: float = 0.6,
    ) -> None:
        self.retries = retries
        self.min_interval = min_interval
        self.max_interval = max(max_interval, min_interval)
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            http2=False,
        )

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        target = random.uniform(self.min_interval, self.max_interval)
        remaining = target - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                response = self._client.request(method, url, **kwargs)
                self._last_request_at = time.monotonic()
                if response.status_code not in RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response
                last_error = httpx.HTTPStatusError(
                    f"retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in RETRYABLE_STATUS:
                    raise
            if attempt < self.retries:
                delay = 0.5 * (2**attempt)
                LOGGER.warning("request retry method=%s url=%s attempt=%d", method, url, attempt + 1)
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("POST", url, **kwargs)


def response_metadata(response: httpx.Response, *, method: str) -> dict[str, object]:
    return {
        "url": str(response.url),
        "method": method.upper(),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
    }
