"""Bounded passive request metadata for read-only FBS investigation.

No HAR, bodies, headers, raw URLs, parameter values or account records are
included in reports. Full candidate URLs exist only in memory during discovery.
"""

import hashlib
import re
from urllib.parse import urlsplit

from .fbs_http import fbs_document_url


class ReadTraffic:
    def __init__(self, page, *, limit=200):
        self.page = page
        self.limit = limit
        self.stage = "session"
        self.records = []
        self.dropped = 0
        self.ignored_assets = 0
        self.total_responses = 0
        self.json_responses = 0
        self.aspnet_postbacks = 0
        self._documents = []

    def __enter__(self):
        self.page.on("response", self._response)
        return self

    def __exit__(self, *_):
        self.page.remove_listener("response", self._response)
        self._documents.clear()

    def _response(self, response):
        request = response.request
        url = request.url
        if not fbs_document_url(url):
            return
        resource = request.resource_type
        if resource not in {"document", "xhr", "fetch"}:
            self.ignored_assets += 1
            return
        self.total_responses += 1
        parsed = urlsplit(url)
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        )
        media = (
            content_type
            if content_type
            in {
                "text/html",
                "application/json",
                "text/plain",
                "text/javascript",
                "application/javascript",
                "text/css",
            }
            else "other"
        )
        method = (
            request.method if request.method in {"GET", "POST", "HEAD"} else "other"
        )
        postback = method == "POST" and "__VIEWSTATE=" in (request.post_data or "")
        self.json_responses += media == "application/json"
        self.aspnet_postbacks += postback
        # Endpoint discovery must keep working even when the public metadata
        # budget is exhausted. Only a small tail of document URLs stays in RAM.
        if method == "GET" and resource == "document" and response.status == 200:
            try:
                frame = request.frame
            except Exception:
                frame = None  # Service-worker responses have no frame.
            # FBS assigns window.name after the document response. Store the
            # frame identity now and match the final content frame when read.
            if frame is not None:
                self._documents.append((frame, url))
                del self._documents[:-16]
        if len(self.records) >= self.limit:
            self.dropped += 1
            return
        # Path segments can contain identity/session tokens. Hash the path and
        # retain only a recognized suffix, never its literal segments or query.
        suffix = re.search(r"\.(aspx|ashx|asmx|json|js|css|html)$", parsed.path, re.I)
        self.records.append(
            {
                "stage": self.stage,
                "endpoint_id": hashlib.sha256(parsed.path.encode()).hexdigest()[:16],
                "extension": suffix[1].lower() if suffix else None,
                "has_query": bool(parsed.query),
                "method": method,
                "resource_type": resource,
                "status": response.status,
                "content_type": media,
                "aspnet_postback": postback,
            }
        )

    def document_url(self, frame):
        return next(
            (
                url
                for candidate, url in reversed(self._documents)
                if candidate is frame and frame.url == url
            ),
            None,
        )

    def report(self):
        return {
            "requests": list(self.records),
            "truncated": self.dropped > 0,
            "dropped_responses": self.dropped,
            "responses_observed": self.total_responses,
            "ignored_asset_responses": self.ignored_assets,
            "json_responses": self.json_responses,
            "aspnet_postbacks": self.aspnet_postbacks,
        }
