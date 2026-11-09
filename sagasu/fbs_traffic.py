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
        if len(self.records) >= self.limit:
            self.dropped += 1
            return
        parsed = urlsplit(url)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
        media = content_type if content_type in {
            "text/html", "application/json", "text/plain", "text/javascript",
            "application/javascript", "text/css",
        } else "other"
        resource = request.resource_type
        if resource not in {"document", "xhr", "fetch", "script", "stylesheet"}:
            resource = "other"
        method = request.method if request.method in {"GET", "POST", "HEAD"} else "other"
        # Path segments can contain identity/session tokens. Hash the path and
        # retain only a recognized suffix, never its literal segments or query.
        suffix = re.search(r"\.(aspx|ashx|asmx|json|js|css|html)$", parsed.path, re.I)
        self.records.append({
            "stage": self.stage,
            "endpoint_id": hashlib.sha256(parsed.path.encode()).hexdigest()[:16],
            "extension": suffix[1].lower() if suffix else None,
            "has_query": bool(parsed.query),
            "method": method,
            "resource_type": resource,
            "status": response.status,
            "content_type": media,
            "aspnet_postback": method == "POST" and "__VIEWSTATE=" in (request.post_data or ""),
        })
        if method == "GET" and resource == "document" and response.status == 200:
            try:
                frame = request.frame
            except Exception:
                # Service-worker requests do not have an associated frame.
                return
            if frame.name == "frameContent":
                self._documents.append((frame, url))

    def document_url(self, frame):
        return next((url for candidate, url in reversed(self._documents) if candidate is frame and frame.url == url), None)

    def report(self):
        return {
            "requests": list(self.records),
            "truncated": self.dropped > 0,
            "dropped_responses": self.dropped,
            "json_responses": sum(r["content_type"] == "application/json" for r in self.records),
            "aspnet_postbacks": sum(r["aspnet_postback"] for r in self.records),
        }
