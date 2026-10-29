import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

RETRIED_STATUSES = frozenset({429, 500, 502, 503, 504})


class NotFound(Exception):
    pass


class JsonClient:
    """GET-only JSON client with Bearer auth and retries on transient failures."""

    def __init__(self, base_url: str, token: str, timeout: float = 180, retries: int = 5):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._retries = retries

    def get(self, path: str, **params: str) -> Any:
        # TeamCity locators are readable with (),: left as is; '+' in dates must be encoded.
        query = urllib.parse.urlencode(params, safe="():,")
        url = f"{self._base}/{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        )
        for attempt in range(1, self._retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(url) from e
                if e.code not in RETRIED_STATUSES or attempt == self._retries:
                    raise
                reason = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == self._retries:
                    raise
                reason = str(e)
            delay = 5 * 2 ** (attempt - 1)
            log.warning("GET %s failed (%s), retry %d in %ds", url, reason, attempt, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")
