from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping

from praha_predictor.config import PipelineConfig


class HttpFetchError(RuntimeError):
    def __init__(self, url: str, failure_class: str, message: str) -> None:
        super().__init__(message)
        self.url = url
        self.failure_class = failure_class


@dataclass
class FetchResponse:
    url: str
    final_url: str
    status_code: int
    text: str
    latency_ms: float


def fetch_text(
    url: str,
    config: PipelineConfig,
    *,
    accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    extra_headers: Mapping[str, str] | None = None,
) -> FetchResponse:
    last_error: HttpFetchError | None = None
    headers = {
        "User-Agent": config.user_agent,
        "Accept": accept,
    }
    if extra_headers:
        headers.update(extra_headers)

    for attempt in range(config.request_retries + 1):
        request = urllib.request.Request(url, headers=headers)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout_seconds) as response:
                text = response.read().decode("utf-8", "ignore")
                latency_ms = (time.perf_counter() - started) * 1000
                return FetchResponse(
                    url=url,
                    final_url=response.geturl(),
                    status_code=getattr(response, "status", 200),
                    text=text,
                    latency_ms=latency_ms,
                )
        except urllib.error.HTTPError as error:
            failure_class = f"http_{error.code}"
            last_error = HttpFetchError(url, failure_class, f"{failure_class} for {url}")
            if error.code in {404, 410}:
                break
        except urllib.error.URLError as error:
            reason_text = str(error.reason).lower()
            failure_class = "timeout" if "timed out" in reason_text else "network_error"
            last_error = HttpFetchError(url, failure_class, f"{failure_class} for {url}")
        except TimeoutError:
            last_error = HttpFetchError(url, "timeout", f"timeout for {url}")

        if attempt < config.request_retries:
            delay = config.retry_backoff_seconds * (2**attempt) + random.uniform(0.05, 0.25)
            time.sleep(delay)

    if last_error is None:
        raise HttpFetchError(url, "unknown_error", f"unknown fetch error for {url}")
    raise last_error

