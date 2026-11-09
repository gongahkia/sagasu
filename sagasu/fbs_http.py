"""Session-backed HTTP reads of observed FBS account documents.

This is not a guessed REST API. Only GET documents observed during a browser
account read are eligible. The router must compare a complete response against
the browser before using it. No postbacks, JavaScript, or action links execute.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .fbs_account import parse_tasks
from .fbs_client import AuthenticationRequired, FBSError, FBS_URL, LayoutChanged

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class HTTPReadUnavailable(FBSError):
    default_reason = "api_read_unavailable"

    def __init__(self, code="unsupported_document"):
        # code is always authored by this module/router, never response text.
        super().__init__(
            "A verified FBS HTTP read is unavailable. Use the browser read backend."
        )
        self.code = code


def fbs_document_url(url):
    """Accept only same-origin HTTPS URLs without credentials or fragments."""
    try:
        parsed = urlsplit(url)
        origin = urlsplit(FBS_URL)
        return (
            parsed.scheme == origin.scheme
            and parsed.hostname == origin.hostname
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and bool(parsed.path)
            and not re.search(r"[\x00-\x20\\]", url)
        )
    except (ValueError, TypeError):
        return False


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: object = field(default=None, repr=False)

    def nodes(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.nodes()

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(
            part.text() if isinstance(part, _Node) else part for part in self.children
        )

    def visible(self):
        node = self
        while node:
            style = re.sub(r"\s+", "", node.attrs.get("style", "").lower())
            if (
                "hidden" in node.attrs
                or node.attrs.get("aria-hidden") == "true"
                or re.search(
                    r"(?:^|;)(?:display:none|visibility:hidden)(?:[;!]|$)", style
                )
            ):
                return False
            node = node.parent
        return True

    def enabled(self):
        return (
            "disabled" not in self.attrs
            and self.attrs.get("aria-disabled") != "true"
            and "disabled" not in self.attrs.get("class", "").lower().split()
        )


class _Document(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.count = 0
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 100000 or len(self.stack) > 100:
            raise HTTPReadUnavailable("document_limit")
        node = _Node(tag, {k: v or "" for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def by_id(self, identifier):
        found = [n for n in self.root.nodes() if n.attrs.get("id") == identifier]
        if len(found) != 1:
            raise HTTPReadUnavailable("unreadable_grid")
        return found[0]


def _text(node):
    return " ".join(node.text().split())


def _actions(row, suffix):
    return [
        node
        for node in row.nodes()
        if node.tag == "a"
        and node.attrs.get("id", "").endswith(suffix)
        and node.visible()
    ]


def parse_account_document(kind, html):
    """Normalize only fully rendered, complete account grids; otherwise reject.

    Client-rendered grids, paging/postback requirements and unknown totals are
    deliberately unsupported. No executable script is evaluated by this parser.
    """
    if kind not in {"bookings", "tasks"}:
        raise HTTPReadUnavailable("unsupported_operation")
    if len(html.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise HTTPReadUnavailable("document_limit")
    document = _Document(html)
    grid_id = "GridViewBooking" if kind == "bookings" else "GridViewTask"
    grid, content = document.by_id(grid_id), document.by_id(grid_id + "_content")
    if not any(n is content for n in grid.nodes()) or not grid.visible():
        raise HTTPReadUnavailable("unreadable_grid")
    rows = [
        node
        for node in content.nodes()
        if node.tag == "tr" and "row" in node.attrs.get("class", "").split()
    ]
    values = [
        [_text(n) for n in row.children if isinstance(n, _Node) and n.tag == "td"]
        for row in rows
    ]
    summary = _text(grid)
    totals = re.findall(r"(\d+)\s+ITEM\(S\)\s+TOTAL", summary, re.I)
    if len(set(totals)) != 1:
        raise HTTPReadUnavailable("unverified_total")
    total = int(totals[0])
    if kind == "tasks":
        headers = [_text(n) for n in grid.nodes() if n.tag == "th"]
        try:
            result = parse_tasks(
                {"headers": headers, "rows": values, "summary": summary}
            )
        except LayoutChanged:
            raise HTTPReadUnavailable("unreadable_grid") from None
        from .fbs_tasks import pending_status

        approvers = [
            n
            for n in document.root.nodes()
            if n.tag == "a"
            and ("href" in n.attrs or n.attrs.get("role") == "link")
            and re.fullmatch(r"Approve Selected", _text(n), re.I)
            and n.visible()
            and n.enabled()
        ]
        data_rows = [row for row, cells in zip(rows, values) if cells != ["No items"]]
        for item, row in zip(result["tasks"], data_rows):
            checkboxes = [
                n
                for n in row.nodes()
                if n.tag == "input"
                and n.attrs.get("type", "").lower() == "checkbox"
                and n.visible()
                and n.enabled()
            ]
            item["can_approve"] = (
                len(approvers) == 1
                and pending_status(item["status"])
                and len(checkboxes) == 1
            )
            item["can_view"] = len(_actions(row, "_BtnView")) == 1
    else:
        bookings = []
        for row, cells in zip(rows, values):
            if len(cells) == 1 and cells[0].casefold() == "no items":
                continue
            if len(cells) < 9 or any(not cells[i] for i in (1, 2, 4, 8)):
                raise HTTPReadUnavailable("unreadable_grid")
            bookings.append(
                {
                    "reference_number": cells[1],
                    "date_time": cells[2],
                    "room": cells[4],
                    "fbs_status": cells[8],
                    "can_cancel": len(_actions(row, "_BtnCancel")) == 1,
                }
            )
        if total == 0 and not re.search(r"\bNo items\b", summary, re.I):
            raise HTTPReadUnavailable("unverified_empty_list")
        result = {
            "bookings": bookings,
            "total": total,
            "complete": len(bookings) == total,
        }
    refs = [r["reference_number"] for r in result[kind]]
    if len(set(refs)) != len(refs) or not result["complete"]:
        raise HTTPReadUnavailable("incomplete_listing")
    return {**result, "pages_read": 1}


async def read_account_http(client, kind, url, *, timeout_ms=10000):
    """One GET through Playwright's shared cookie jar. Never follow redirects."""
    if not fbs_document_url(url):
        raise HTTPReadUnavailable("untrusted_endpoint")
    response = await client.context.request.get(
        url,
        timeout=timeout_ms,
        max_redirects=0,
        max_retries=0,
        fail_on_status_code=False,
    )
    try:
        if response.status in {301, 302, 303, 307, 308, 401, 403}:
            raise AuthenticationRequired(
                "Renew the saved FBS session before HTTP reads."
            )
        if response.status != 200 or response.url != url:
            raise HTTPReadUnavailable("http_response_rejected")
        if (
            response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
            != "text/html"
        ):
            raise HTTPReadUnavailable("unsupported_content_type")
        size = response.headers.get("content-length", "")
        if size.isdigit() and int(size) > MAX_DOCUMENT_BYTES:
            raise HTTPReadUnavailable("document_limit")
        body = await response.body()
        if len(body) > MAX_DOCUMENT_BYTES:
            raise HTTPReadUnavailable("document_limit")
        try:
            html = body.decode("utf-8-sig")
        except UnicodeError:
            raise HTTPReadUnavailable("unsupported_encoding") from None
        return parse_account_document(kind, html)
    finally:
        await response.dispose()
