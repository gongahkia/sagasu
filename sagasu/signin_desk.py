"""The server's own SMU sign-in page, for whoever holds an access key.

A person opens https://HOST/sign-in (or arrives there from an app's OAuth
sign-in), enters their access key once, and then signs in to SMU on a live
picture of Microsoft's sign-in page, running in this copy's browser. They tap
the picture and type into a box; nothing else in the browser is reachable.

    GET  /sign-in            the key form, or the live sign-in once a key is accepted
    POST /sign-in            check the key and start a visit (a cookie)
    POST /sign-in/start      start the SMU sign-in, or join the one running
    GET  /sign-in/status     what to show, and where to go once signed in
    GET  /sign-in/frame      a picture of Microsoft's page while signing in
    POST /sign-in/input      a tap, some text or one key for Microsoft's page
    POST /sign-in/cancel     stop
    GET  /sign-in/view.js    the page's script

What a person types goes to Microsoft's page and nowhere else: it is never
logged or stored. The key is checked here and never shown again.
"""

import asyncio
import hmac
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .live_view import LIVE_VIEW, LiveView
from .oauth import (
    NO_STORE,
    PAGE,
    WRONG_KEY_WINDOW,
    WRONG_KEYS_PER_ADDRESS,
    Limiter,
    digest,
)
from .observability import event

DESK_PATH = "/sign-in"
COOKIE = "sagasu_sign_in"
VISIT_SECONDS = 20 * 60
# No word from the page for this long: the person has gone, so stop the
# sign-in rather than keep other requests waiting behind it.
IDLE_SECONDS = 90
WATCH_INTERVAL = 5
FRAME_INTERVAL = 0.25
TEXT_LIMIT = 256
BODY_LIMIT = 2048
KEYS = frozenset({"Enter", "Backspace", "Tab", "Escape"})

HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
        "img-src 'self' blob:; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}

# What to tell the person at each step Microsoft shows (fbs_client states).
STEPS = {
    "email": "Type your SMU email in the box and press Send.",
    "account": "Tap your account in the picture, or type your SMU email and press Send.",
    "password": "Type your password in the box and press Send.",
    "password_choice": "Choose how to sign in, in the picture.",
    "method_picker": "Choose Microsoft Authenticator in the picture, if it is offered.",
    "webauthn": "Choose another way to sign in, in the picture.",
    "authenticator": "Approve the sign-in in Microsoft Authenticator on your phone. If the picture shows a number, enter it in the app.",
    "stay_signed_in": "Tap Yes to stay signed in for longer, or No.",
    "manual": "Follow the steps in the picture.",
    "failed": "Microsoft did not accept that. Follow the steps in the picture.",
}
FAILURES = {
    "authentication_timeout": "Sign-in took too long and was stopped.",
    "sign_in_rejected": "Microsoft did not accept the sign-in.",
    "authentication_interrupted": "The sign-in was interrupted.",
}


@dataclass
class Visit:
    label: str
    expires: float
    continuation: dict | None = None  # an app's OAuth sign-in, waiting on SMU
    next_url: str | None = None


class SignInDesk:
    def __init__(self, service, keys, issuer, *, clock=time.time):
        self.service = service
        self.keys = keys  # SHA-256 digest of each API key -> label
        self.issuer = issuer
        self.url = issuer + DESK_PATH
        # The page names the copy, so nobody signs in to someone else's.
        self.view_page = VIEW_PAGE.replace(
            "@HOST@", html.escape(urlsplit(issuer).hostname or "")
        )
        self.oauth = None  # set when apps may sign in through OAuth
        self.clock = clock
        # One visit at a time: a newer one replaces the older.
        self.visit_digest = None
        self.visit = None
        self.job_id = None
        self.view = None
        self.last_seen = 0.0
        self.watchdog = None
        self.frame = (0.0, None)
        self.frame_lock = asyncio.Lock()
        self.input_lock = asyncio.Lock()
        self.wrong_keys = Limiter(WRONG_KEYS_PER_ADDRESS, WRONG_KEY_WINDOW)

    async def close(self):
        if self.watchdog is not None:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)
            self.watchdog = None

    # -- visits ------------------------------------------------------------------

    def begin(self, request, label, continuation=None):
        """Start a visit for a person whose key was just accepted."""
        token = secrets.token_urlsafe(32)
        self.visit_digest = digest(token)
        self.visit = Visit(label, self.clock() + VISIT_SECONDS, continuation)
        self.last_seen = self.clock()
        event("signin_desk.visit", key=label, app=continuation is not None)
        response = RedirectResponse(DESK_PATH, status_code=303, headers=NO_STORE)
        response.set_cookie(
            COOKIE,
            token,
            max_age=VISIT_SECONDS,
            path=DESK_PATH,
            # Behind the proxy every request is HTTPS; a bare local test is not.
            secure=request.headers.get("x-forwarded-proto") == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    def _visit(self, request):
        token = request.cookies.get(COOKIE) or ""
        visit = self.visit
        if (
            not token
            or visit is None
            or self.visit_digest is None
            or not hmac.compare_digest(digest(token), self.visit_digest)
        ):
            return None
        if visit.expires <= self.clock():
            self.visit = self.visit_digest = None
            return None
        self.last_seen = self.clock()
        return visit

    def _same_site(self, request, strict):
        """A form post may arrive with Origin "null"; a script's never does."""
        origin = request.headers.get("origin")
        if origin is None or origin == "null":
            return not strict
        return urlsplit(origin).hostname == urlsplit(self.issuer).hostname

    @staticmethod
    def _address(request):
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )

    # -- the login job -----------------------------------------------------------

    def _job(self):
        return self.service.jobs.get(self.job_id) if self.job_id else None

    def _start_job(self):
        self.view = LiveView()
        self.frame = (0.0, None)
        # The job is local: it may show Microsoft's pages and wait for a
        # person, because the person is right here, watching it.
        token = LIVE_VIEW.set(self.view)
        try:
            job = self.service.start("login")
        finally:
            LIVE_VIEW.reset(token)
        self.job_id = job["operation_id"]
        if self.watchdog is None or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watch())

    @staticmethod
    def _ended(job):
        # A job cancelled before it began never records an outcome.
        return job.terminal or (job.task is not None and job.task.done())

    async def _watch(self):
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            job = self._job()
            if job is None or self._ended(job):
                return
            if self.clock() - self.last_seen > IDLE_SECONDS:
                event("signin_desk.abandoned", level=logging.WARNING)
                if job.task is not None:
                    job.task.cancel()
                return

    def _live_page(self):
        from .fbs_client import FBSClient

        job, view = self._job(), self.view
        page = view.page if view is not None else None
        if (
            job is None
            or self._ended(job)
            or page is None
            or page.is_closed()
            or not FBSClient._trusted_login_url(page.url)
        ):
            return None
        return page

    def _status(self, visit):
        job = self._job()
        live = self._live_page() is not None
        state = self.view.state if self.view is not None else None
        status = {
            "phase": "starting",
            "message": "Opening SMU sign-in…",
            "picture": False,
            "field": "text",
            "retry": False,
            "next": None,
        }
        if job is None:
            status.update(phase="idle", message="Ready to sign in.", retry=True)
        elif job.status == "succeeded":
            status.update(phase="signed_in", message="You're signed in to SMU.")
            if visit.continuation is not None and self.oauth is not None:
                if visit.next_url is None:
                    visit.next_url = self.oauth.finish(visit.continuation, visit.label)
                status["message"] = (
                    "You're signed in to SMU. Taking you back to the app…"
                )
                status["next"] = visit.next_url
            else:
                status["message"] = (
                    "You're signed in to SMU. Your agents can now use FBS as you. "
                    "You can close this page."
                )
        elif job.status == "cancelled" or (self._ended(job) and not job.terminal):
            status.update(phase="stopped", message="Sign-in stopped.", retry=True)
        elif job.terminal:
            reason = (job.error or {}).get("reason")
            status.update(
                phase="failed",
                message=FAILURES.get(reason, "Sign-in did not finish.")
                + " Try again when you're ready. If SMU or Microsoft showed an error"
                " or a block page, stop and check with SMU IT first.",
                retry=True,
            )
        elif live:
            status.update(
                phase="signing_in",
                picture=True,
                message=STEPS.get(state, "Follow the steps in the picture."),
                field={"password": "password", "email": "email"}.get(state, "text"),
            )
            if job.status == "awaiting_mfa":
                status["message"] = STEPS["authenticator"]
        elif job.status == "queued":
            # Running jobs hold the queue; this one has not reached it yet.
            if self.service.queue.locked():
                status["message"] = "Waiting for the server to finish another request…"
        else:
            status["message"] = "Checking your SMU sign-in…"
        return status

    # -- routes ------------------------------------------------------------------

    async def page(self, request: Request):
        if request.method == "POST":
            return await self._key_posted(request)
        if self._visit(request) is None:
            return self._key_form()
        return HTMLResponse(self.view_page, headers=HEADERS)

    async def _key_posted(self, request):
        # Strict: a key posted from another site could open a sign-in to a copy
        # that is not the visitor's. This page's own post always names itself.
        if not self._same_site(request, strict=True):
            return self._key_form("This form can only be sent from its own page.", 403)
        address = self._address(request)
        if self.wrong_keys.used_up(address):
            return self._key_form(
                "Too many wrong keys. Wait ten minutes, then try again.", 429
            )
        form = await request.form()
        from .remote import check_key

        label, _ = check_key(self.keys, {"x-api-key": str(form.get("key") or "")})
        if label is None:
            self.wrong_keys.record(address)
            event("signin_desk.wrong_key", level=logging.WARNING)
            return self._key_form(
                "That key was not accepted. Check it and try again.", 400
            )
        return self.begin(request, label)

    def _key_form(self, error=None, status=200):
        body = KEY_BODY.format(
            error=(
                f'<p class="error" role="alert">{html.escape(error)}</p>'
                if error
                else ""
            )
        )
        return HTMLResponse(
            PAGE.format(title="Sign in to SMU", body=body),
            status_code=status,
            headers=HEADERS,
        )

    async def _json_call(self, request):
        """The visit for a script's POST from this page, else a refusal."""
        if not self._same_site(request, strict=True) or not request.headers.get(
            "content-type", ""
        ).startswith("application/json"):
            return None, JSONResponse({"error": "not_allowed"}, 403, headers=NO_STORE)
        visit = self._visit(request)
        if visit is None:
            return None, JSONResponse({"error": "expired"}, 401, headers=NO_STORE)
        body = await request.body()
        if len(body) > BODY_LIMIT:
            return None, JSONResponse({"error": "too_large"}, 413, headers=NO_STORE)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return None, JSONResponse({"error": "bad_request"}, 400, headers=NO_STORE)
        return (visit, data), None

    async def start(self, request: Request):
        called, refusal = await self._json_call(request)
        if refusal is not None:
            return refusal
        visit, _ = called
        job = self._job()
        if job is None or self._ended(job):
            from .mcp_service import AgentError

            try:
                self._start_job()
            except AgentError:
                return JSONResponse({"error": "busy"}, 503, headers=NO_STORE)
            visit.next_url = None
        return JSONResponse(self._status(visit), headers=NO_STORE)

    async def status(self, request: Request):
        visit = self._visit(request)
        if visit is None:
            return JSONResponse({"error": "expired"}, 401, headers=NO_STORE)
        return JSONResponse(self._status(visit), headers=NO_STORE)

    async def picture(self, request: Request):
        if self._visit(request) is None:
            return Response(status_code=401, headers=NO_STORE)
        page = self._live_page()
        if page is None:
            self.frame = (0.0, None)
            return Response(status_code=204, headers=NO_STORE)
        async with self.frame_lock:
            taken, data = self.frame
            if data is None or self.clock() - taken >= FRAME_INTERVAL:
                try:
                    data = await page.screenshot(type="jpeg", quality=70, timeout=5000)
                except Exception:
                    return Response(status_code=204, headers=NO_STORE)
                self.frame = (self.clock(), data)
        return Response(data, media_type="image/jpeg", headers=NO_STORE)

    async def input(self, request: Request):
        called, refusal = await self._json_call(request)
        if refusal is not None:
            return refusal
        _, data = called
        kind = data.get("kind")
        page = self._live_page()
        if page is None:
            return JSONResponse({"error": "not_signing_in"}, 409, headers=NO_STORE)
        if kind == "tap":
            x, y = data.get("x"), data.get("y")
            if not all(
                type(value) in (int, float) and 0 <= value <= 1 for value in (x, y)
            ):
                return JSONResponse({"error": "bad_request"}, 400, headers=NO_STORE)
            size = page.viewport_size or {"width": 0, "height": 0}
            action = page.mouse.click(x * size["width"], y * size["height"])
        elif kind == "text":
            text = data.get("text")
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= TEXT_LIMIT
                or not text.isprintable()
            ):
                return JSONResponse({"error": "bad_request"}, 400, headers=NO_STORE)
            action = page.keyboard.type(text, delay=10)
        elif kind == "key" and data.get("key") in KEYS:
            action = page.keyboard.press(data["key"])
        else:
            return JSONResponse({"error": "bad_request"}, 400, headers=NO_STORE)
        async with self.input_lock:
            try:
                await action
            except Exception:
                return JSONResponse({"error": "not_signing_in"}, 409, headers=NO_STORE)
        self.frame = (0.0, None)  # show the result at once
        return Response(status_code=204, headers=NO_STORE)

    async def cancel(self, request: Request):
        called, refusal = await self._json_call(request)
        if refusal is not None:
            return refusal
        visit, _ = called
        job = self._job()
        if job is not None and not self._ended(job):
            job.task.cancel()
        back = None
        if visit.continuation is not None and self.oauth is not None:
            back = self.oauth.refuse(visit.continuation)
        self.visit = self.visit_digest = None
        event("signin_desk.cancelled")
        return JSONResponse({"next": back}, headers=NO_STORE)

    async def script(self, request: Request):
        return Response(
            VIEW_SCRIPT,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    def routes(self):
        return [
            Route(DESK_PATH, endpoint=self.page, methods=["GET", "POST"]),
            Route(DESK_PATH + "/start", endpoint=self.start, methods=["POST"]),
            Route(DESK_PATH + "/status", endpoint=self.status, methods=["GET"]),
            Route(DESK_PATH + "/frame", endpoint=self.picture, methods=["GET"]),
            Route(DESK_PATH + "/input", endpoint=self.input, methods=["POST"]),
            Route(DESK_PATH + "/cancel", endpoint=self.cancel, methods=["POST"]),
            Route(DESK_PATH + "/view.js", endpoint=self.script, methods=["GET"]),
        ]


def is_desk_path(path):
    return path == DESK_PATH or path.startswith(DESK_PATH + "/")


KEY_BODY = """<h1>Sign in to SMU</h1>
<p>Enter the access key you were given for this Sagasu server. Next, you sign in to your own SMU account, and your agents can then use FBS as you.</p>
{error}
<form method="post" autocomplete="off">
<label for="key">Access key</label>
<input id="key" name="key" type="password" required autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" autofocus>
<button type="submit">Continue</button>
</form>"""

VIEW_BODY = """<h1>Sign in to SMU</h1>
<p class="muted">For the Sagasu copy at <strong>@HOST@</strong>. Only sign in here if that is your own copy.</p>
<p id="message" role="status" aria-live="polite">Opening SMU sign-in…</p>
<form id="typing" autocomplete="off" hidden>
<div class="row">
<input id="text" type="text" aria-label="Type here" placeholder="Type here" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
<button type="submit" id="send" aria-label="Type this and press Enter">Send</button>
<button type="button" id="erase" class="secondary small" aria-label="Delete one character">⌫</button>
</div>
</form>
<div id="screen" class="screen" tabindex="0" hidden aria-label="Microsoft sign-in page. Click or tap to press; on a computer you can also type here.">
<img id="picture" alt="" draggable="false">
</div>
<p id="waiting" class="waiting">One moment…</p>
<button type="button" id="retry" hidden>Try again</button>
<p class="muted">This is a live picture of Microsoft's sign-in page, running in the browser on your Sagasu server. What you type goes only to that page and is not kept.</p>
<button type="button" id="cancel" class="secondary">Cancel</button>"""

VIEW_STYLE = """
main { max-width: 560px; }
.desk .row { display: flex; gap: 8px; align-items: stretch; }
.desk .row input { flex: 1; min-width: 0; }
.desk .row button { width: auto; margin-top: 0; padding: 12px 18px; }
.desk button.small { padding: 12px 14px; }
.screen { margin: 14px 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  background: #fff; cursor: pointer; touch-action: manipulation; }
.screen:focus { outline: 3px solid var(--accent); outline-offset: 2px; }
.screen img { display: block; width: 100%; height: auto; user-select: none; -webkit-user-select: none; }
.waiting { color: var(--muted); }
#message { font-weight: 600; }
"""

VIEW_SCRIPT = r"""'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const message = $('message'), typing = $('typing'), text = $('text'),
    screen = $('screen'), picture = $('picture'), waiting = $('waiting'),
    retry = $('retry'), cancel = $('cancel'), erase = $('erase');
  let polling = false, framing = false, wanted = false, finished = false;
  let chain = Promise.resolve(), pending = '', flushTimer = null, shown = null;

  const post = (path, body) => fetch(path, {
    method: 'POST', cache: 'no-store', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  const send = (body) => { chain = chain.then(() => post('/sign-in/input', body)).catch(() => {}); return chain; };

  function render(status) {
    message.textContent = status.message;
    wanted = !!status.picture;
    typing.hidden = screen.hidden = !wanted;
    waiting.hidden = wanted || status.phase === 'signed_in' || status.retry;
    retry.hidden = !status.retry;
    cancel.hidden = status.phase === 'signed_in';
    if (text.type !== status.field) text.type = status.field;
    if (wanted && !framing) frames();
  }

  function stop(note) {
    finished = true; wanted = false;
    typing.hidden = screen.hidden = waiting.hidden = retry.hidden = cancel.hidden = true;
    if (note) message.textContent = note;
  }

  async function poll() {
    if (polling) return;
    polling = true;
    while (!finished) {
      try {
        const response = await fetch('/sign-in/status', {cache: 'no-store', credentials: 'same-origin'});
        if (response.status === 401) { stop('This sign-in page has expired. Open the link again to start over.'); break; }
        const status = await response.json();
        render(status);
        if (status.next) { stop(status.message); location.replace(status.next); break; }
        if (status.phase === 'signed_in') { stop(status.message); break; }
      } catch (error) {
        message.textContent = 'Connection problem. Retrying…';
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    polling = false;
  }

  async function frames() {
    framing = true;
    while (wanted && !finished) {
      try {
        const response = await fetch('/sign-in/frame', {cache: 'no-store', credentials: 'same-origin'});
        if (response.status === 200) {
          const url = URL.createObjectURL(await response.blob());
          picture.src = url;
          if (shown) URL.revokeObjectURL(shown);
          shown = url;
        }
      } catch (error) {}
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    framing = false;
  }

  async function begin() {
    retry.hidden = true;
    waiting.hidden = false;
    message.textContent = 'Opening SMU sign-in…';
    try {
      const response = await post('/sign-in/start');
      if (response.status === 401) { stop('This sign-in page has expired. Open the link again to start over.'); return; }
      if (response.ok) render(await response.json());
      else message.textContent = 'The server is busy. Try again in a minute.';
    } catch (error) {
      message.textContent = 'Connection problem. Check your connection and try again.';
      retry.hidden = false;
    }
    poll();
  }

  function flush() {
    flushTimer = null;
    if (pending) { send({kind: 'text', text: pending}); pending = ''; }
  }

  screen.addEventListener('click', (event) => {
    const box = picture.getBoundingClientRect();
    if (!picture.naturalWidth || !box.width) return;
    const x = (event.clientX - box.left) / box.width, y = (event.clientY - box.top) / box.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    flush();
    send({kind: 'tap', x, y});
    if (!matchMedia('(pointer: coarse)').matches) screen.focus();
  });
  screen.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (['Enter', 'Backspace', 'Tab', 'Escape'].includes(event.key)) {
      event.preventDefault(); flush(); send({kind: 'key', key: event.key});
    } else if (event.key.length === 1) {
      event.preventDefault();
      pending += event.key;
      if (pending.length >= 200) flush();
      else if (!flushTimer) flushTimer = setTimeout(flush, 120);
    }
  });
  screen.addEventListener('paste', (event) => {
    const pasted = (event.clipboardData && event.clipboardData.getData('text')) || '';
    if (pasted) { event.preventDefault(); flush(); send({kind: 'text', text: pasted.slice(0, 256)}); }
  });
  typing.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = text.value;
    text.value = '';
    if (value) send({kind: 'text', text: value.slice(0, 256)});
    send({kind: 'key', key: 'Enter'});
  });
  erase.addEventListener('click', () => send({kind: 'key', key: 'Backspace'}));
  retry.addEventListener('click', begin);
  cancel.addEventListener('click', async () => {
    try {
      const response = await post('/sign-in/cancel');
      const answer = response.ok ? await response.json() : {};
      stop('Sign-in cancelled.');
      if (answer.next) location.replace(answer.next);
    } catch (error) { stop('Sign-in cancelled.'); }
  });
  begin();
})();
"""

VIEW_PAGE = (
    PAGE.format(title="Sign in to SMU", body=VIEW_BODY)
    .replace("</style>", VIEW_STYLE + "</style>", 1)
    .replace('<div class="card">', '<div class="card desk">', 1)
    .replace("</main>", '</main>\n<script src="/sign-in/view.js"></script>', 1)
)
