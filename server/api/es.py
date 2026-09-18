"""Search-only access to Elasticsearch.

Deliberately narrow: it can run a search and fetch a document, and nothing
else. The cluster is never reachable from the browser, so what this module
exposes is the whole surface the public can touch.

Only the standard library is used, so the site needs nothing installed but
Django itself.
"""

import base64
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings

# Highlighting comes back wrapped in two characters that cannot occur in a
# manuscript, rather than in HTML. The API then hands the browser a list of
# plain strings and a flag, and nothing it renders can be markup.
HL_OPEN = ""
HL_CLOSE = ""

_local = threading.local()


class SearchError(RuntimeError):
    """Elasticsearch could not be reached, or refused the query."""


def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    if settings.ES_API_KEY:
        headers["Authorization"] = f"ApiKey {settings.ES_API_KEY}"
    elif settings.ES_USER:
        raw = f"{settings.ES_USER}:{settings.ES_PASSWORD or ''}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")

    return headers


def _context() -> ssl.SSLContext | None:
    context = getattr(_local, "context", False)

    if context is False:
        if not settings.ES_VERIFY:
            context = ssl._create_unverified_context()
        elif settings.ES_CA_CERT:
            context = ssl.create_default_context(cafile=settings.ES_CA_CERT)
        else:
            context = None

        _local.context = context

    return context


def _call(method: str, path: str, body: Any = None) -> Any:
    url = settings.ES_URL.rstrip("/") + path

    data = None

    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers=_auth_headers(),
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.ES_TIMEOUT,
            context=_context(),
        ) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None

    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise SearchError(
            f"Elasticsearch refused the request (HTTP {error.code}): {detail}"
        ) from error

    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SearchError(f"Elasticsearch is not answering ({error})") from error


def search(body: dict[str, Any]) -> dict[str, Any]:
    index = urllib.parse.quote(settings.ES_INDEX, safe="")
    return _call("POST", f"/{index}/_search", body)


def get_document(document_id: str) -> dict[str, Any] | None:
    index = urllib.parse.quote(settings.ES_INDEX, safe="")
    quoted = urllib.parse.quote(document_id, safe="")

    try:
        found = _call("GET", f"/{index}/_doc/{quoted}")
    except SearchError as error:
        if "HTTP 404" in str(error):
            return None
        raise

    return (found or {}).get("_source")


def cluster_version() -> str:
    info = _call("GET", "/") or {}
    return info.get("version", {}).get("number", "?")


def highlight_parts(marked: str | None, plain: str) -> list[list]:
    """Turn a highlighted field into [[text, is_match], ...].

    Elasticsearch does not escape what it wraps, so the marks travel as control
    characters and are split apart here. The browser then builds text nodes and
    never HTML, and a manuscript cannot inject anything into the page.
    """

    if not marked:
        return [[plain, False]] if plain else []

    parts: list[list] = []
    buffer = ""
    hot = False

    for character in marked:
        if character in (HL_OPEN, HL_CLOSE):
            if buffer:
                parts.append([buffer, hot])

            buffer = ""
            hot = character == HL_OPEN
        else:
            buffer += character

    if buffer:
        parts.append([buffer, hot])

    return parts
