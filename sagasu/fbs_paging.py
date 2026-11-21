"""Bounded traversal of verified FBS account grids, including action lookup."""

import asyncio
from .fbs_client import LayoutChanged
from .observability import event

# Only controls inside the requested account grid can be clicked. Do not execute
# JavaScript from a table cell or assume a missing row means a cancelled booking.
PAGER = r"""(root, direction) => {
    const controls = [...root.querySelectorAll('a,button,input[type=button],input[type=image]')];
    const visible = e => e.getBoundingClientRect().height > 0 &&
        !e.disabled && e.getAttribute('aria-disabled') !== 'true' &&
        !/(?:^|\s)disabled(?:\s|$)/i.test(e.className);
    const labels = e => [e.getAttribute('aria-label'), e.title, e.alt, e.innerText, e.value]
        .filter(Boolean).map(x => x.trim());
    const explicit = controls.findIndex(e => visible(e) && (
        labels(e).some(s => direction === 'first'
            ? /^(?:first(?: page)?|page 1|<<|«|\|<)$/i.test(s)
            : /^(?:next(?: page)?\s*>?|>|»|›)$/i.test(s)) ||
        (e.getAttribute('href') || '').includes('Page$' + (direction === 'first' ? 'First' : 'Next'))));
    if (explicit >= 0) return explicit;
    if (direction === 'first') return controls.findIndex(e => visible(e) && labels(e).includes('1'));
    const selected = [...root.querySelectorAll('[aria-current=page],.pager .selected,.pager .current,.pager span')]
        .map(e => e.innerText.trim()).filter(s => /^\d+$/.test(s));
    if (selected.length !== 1) return -1;
    return controls.findIndex(e => visible(e) && labels(e).includes(String(Number(selected[0]) + 1)));
}"""


async def move_page(client, frame, grid, direction):
    root = frame.locator("#" + grid)
    index = await root.evaluate(PAGER, direction)
    if not isinstance(index, int) or index < 0:
        event("paging.control_unavailable", direction=direction)
        return False
    controls = root.locator("a,button,input[type=button],input[type=image]")
    event("paging.move_started", direction=direction)
    await controls.nth(index).click()
    await client._wait_for_idle(frame)
    event("paging.move_completed", direction=direction)
    return True


async def collect_pages(
    client,
    frame,
    grid,
    key,
    read_page,
    *,
    reference=None,
    max_pages=100,
    identity_key="reference_number",
):
    """Aggregate verified pages or leave the exact requested reference visible.

    A partial result is explicit if the UI has no recognizable next control or
    the page budget is reached. Changing totals, duplicate references, and stuck
    navigation fail closed rather than claiming a complete list.
    """
    async with asyncio.timeout(90):
        await move_page(client, frame, grid, "first")
        records = {}
        signatures = set()
        total = None
        for page in range(1, max_pages + 1):
            result = await read_page()
            if total is None:
                total = result["total"]
            event(
                "paging.page_read",
                page=page,
                row_count=len(result[key]),
                reported_total=result["total"],
                records_read=len(records),
            )
            if result["total"] != total:
                event("paging.total_changed", page=page)
                raise LayoutChanged(
                    "FBS account total changed during pagination; refresh the listing"
                )
            rows = result[key]
            refs = tuple(row[identity_key] for row in rows)
            if refs in signatures or len(set(refs)) != len(refs):
                event("paging.repeated_page_or_reference", page=page)
                raise LayoutChanged(
                    "FBS account pagination repeated a page or reference"
                )
            signatures.add(refs)
            for row in rows:
                ref = row[identity_key]
                if ref in records:
                    event("paging.overlapping_reference", page=page)
                    raise LayoutChanged(
                        "FBS account references overlapped across pages; refresh the listing"
                    )
                records[ref] = row
            if len(records) > total:
                raise LayoutChanged("FBS account rows exceeded the verified total")
            if reference and reference in refs:
                return {**result, "pages_read": page, "reference_found": True}
            if len(records) == total:
                return {
                    key: list(records.values()),
                    "total": total,
                    "complete": True,
                    "pages_read": page,
                }
            if page == max_pages or not await move_page(client, frame, grid, "next"):
                return {
                    key: list(records.values()),
                    "total": total,
                    "complete": False,
                    "pages_read": page,
                    "coverage_reason": (
                        "page_limit"
                        if page == max_pages
                        else "unsupported_or_missing_next_page"
                    ),
                    "next_action": "The FBS listing is partial. Do not claim all records have been checked; refine the listing or inspect FBS manually.",
                }
