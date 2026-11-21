"""A person signing in to SMU from the server's own web sign-in page.

Hosted path only (see SETUP.md, "Hosting for a group"): used by the web
sign-in page. Inert on the local path.

The sign-in page (signin_desk.py) starts an ordinary interactive login job with
a LiveView in LIVE_VIEW. While that job waits on Microsoft's pages, the browser
code attaches its page here, so the person can see the page and tap and type
into it. Nothing else on the server is reachable this way: the view offers a
picture of the page and a few kinds of input, never the browser's address bar,
other tabs, or any page outside Microsoft's sign-in.
"""


from ..observability import event

from ..auth_context import LIVE_VIEW

# Microsoft's sign-in pages switch to their narrow layout below about 600px,
# which reads well both on a phone and in a desktop browser.
VIEWPORT = {"width": 480, "height": 700}


class LiveView:
    """The sign-in page a person is watching, while a login job is on it."""

    def __init__(self):
        self.page = None
        self.state = None
        self._context = None
        self._restore = None

    async def attach(self, page, context):
        self.page, self._context = page, context
        self._restore = page.viewport_size
        try:
            await page.set_viewport_size(VIEWPORT)
        except Exception:
            pass
        # A link that opens a new tab would show nothing to the person and
        # leave a page behind; close any such tab at once.
        if context is not None:
            context.on("page", self._close_new_page)
        event("signin_desk.view_attached")

    async def detach(self):
        page, context, restore = self.page, self._context, self._restore
        self.page = self._context = self._restore = None
        if context is not None:
            try:
                context.remove_listener("page", self._close_new_page)
            except Exception:
                pass
        if page is not None and restore:
            try:
                await page.set_viewport_size(restore)
            except Exception:
                pass

    @staticmethod
    async def _close_new_page(page):
        try:
            await page.close()
        except Exception:
            pass
