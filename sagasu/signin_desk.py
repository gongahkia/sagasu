"""The server's own SMU sign-in page, for whoever holds an access key.

A person reaches https://HOST/sign-in from a one-time link their agent gave
them, from an app's OAuth sign-in, or by entering their access key. There they
save their own SMU email and password once. This copy then signs in with them,
and the person approves the sign-in in Microsoft Authenticator: here the first
time (the number is shown on this page), and from chat after that (the agent
shows the number; see mcp_service and saved_sign_in).

When this copy cannot keep a saved sign-in (no key for it on the server), or
Microsoft asks for a step that cannot be done this way, the page falls back to
a live picture of Microsoft's sign-in page in this copy's browser, which the
person taps and types into.

    GET  /sign-in            the page: key form, link confirmation, or sign-in
    POST /sign-in            check a key or a one-time link, and start a visit
    POST /sign-in/start      sign in: with an email and password to save, with
                             the saved ones, or on the live picture
    POST /sign-in/forget     delete the saved email and password
    GET  /sign-in/status     what to show, and where to go once signed in
    GET  /sign-in/frame      a picture of Microsoft's page (live picture only)
    POST /sign-in/input      a tap, some text or one key (live picture only)
    POST /sign-in/cancel     stop
    GET  /sign-in/view.js    the page's script

A password typed here goes to Microsoft's page and, once Microsoft accepts it,
into the encrypted saved sign-in; it is never logged, shown or returned. The
key is checked here and never shown again.
"""

import asyncio
import hmac
import html
import json
import logging
import re
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
from .saved_sign_in import (
    SAVED_CREDENTIALS,
    Credentials,
    SavedSignInError,
    valid_password,
)

DESK_PATH = "/sign-in"
COOKIE = "sagasu_sign_in"
VISIT_SECONDS = 20 * 60
# One-time links an agent hands its person: short-lived, used once.
LINK_SECONDS = 15 * 60
# No word from the page for this long: the person has gone, so stop the
# sign-in rather than keep other requests waiting behind it.
IDLE_SECONDS = 90
WATCH_INTERVAL = 5
FRAME_INTERVAL = 0.25
TEXT_LIMIT = 256
BODY_LIMIT = 2048
KEYS = frozenset({"Enter", "Backspace", "Tab", "Escape"})
NUMBER = re.compile(r"\bnumber ([0-9]{2,3})\b")

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

# What to tell the person at each step Microsoft shows (fbs_client states),
# when they are working on the live picture.
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
    "failed": (
        "Microsoft did not accept that. If it says the password is incorrect, "
        "type it again carefully in the box and press Send."
    ),
}
# The kind of box to type into at each step. A password box hides what is
# typed, and phone keyboards do not autocorrect or capitalise it.
FIELDS = {"email": "email", "password": "password", "failed": "password"}
FAILURES = {
    "authentication_timeout": "Sign-in took too long and was stopped.",
    "sign_in_rejected": "Microsoft did not accept the sign-in.",
    "authentication_interrupted": "The sign-in was interrupted.",
    "saved_password_rejected": (
        "Microsoft says the email or password is incorrect, so nothing was saved. "
        "Check them and try again."
    ),
    "sign_in_not_approved": (
        "The sign-in was not approved in Microsoft Authenticator in time. Try again "
        "when you have your phone."
    ),
    "sign_in_blocked": (
        "Microsoft has locked or blocked sign-in for this account. Check with SMU IT "
        "before trying again."
    ),
    "sign_in_unfinished": (
        "Microsoft asked for a step this page cannot do. You can finish on "
        "Microsoft's own page instead."
    ),
}
OUTCOMES = {
    "signed_in": "signed in",
    "saved_password_rejected": "password not accepted",
    "sign_in_not_approved": "not approved in time",
    "sign_in_blocked": "blocked by Microsoft",
    "sign_in_needs_page": "needed this page",
    "sign_in_unfinished": "did not finish",
}
SAVED_HINT = (
    "Your agents can now use FBS as you. When SMU asks you to sign in again, "
    "your agent will show you a number to enter in Microsoft Authenticator."
)


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
        self.host = urlsplit(issuer).hostname or ""
        # The page names the copy, so nobody signs in to someone else's.
        self.view_page = VIEW_PAGE.replace("@HOST@", html.escape(self.host))
        self.oauth = None  # set when apps may sign in through OAuth
        self.clock = clock
        # One visit at a time: a newer one replaces the older.
        self.visit_digest = None
        self.visit = None
        self.job_id = None
        self.job_how = None
        self.save_error = None
        self.saved_unchecked = False
        self.save_pending = False
        self.job_save = None
        self.saves_done = {}  # job id -> saved already (each job saves once)
        self.view = None
        self.last_seen = 0.0
        self.watchdog = None
        self.frame = (0.0, None)
        self.frame_lock = asyncio.Lock()
        self.input_lock = asyncio.Lock()
        self.wrong_keys = Limiter(WRONG_KEYS_PER_ADDRESS, WRONG_KEY_WINDOW)
        # The one live one-time link: (token, expiry). Repeated failures give
        # the same link until it is used or expires, so a later failure never
        # replaces the link the person was just given.
        self.live_link = None

    async def close(self):
        if self.watchdog is not None:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)
            self.watchdog = None

    @property
    def store(self):
        """The saved sign-in, when this copy can keep one."""
        store = getattr(self.service, "saved_sign_in", None)
        return store if store is not None and store.available else None

    # -- one-time links ------------------------------------------------------------

    def link(self):
        """A link that opens this page once, without the access key.

        Made for an agent's error result, so the person it belongs to can save
        or fix their sign-in without finding their key again. The page then
        accepts only this copy's own SMU account.
        """
        now = self.clock()
        # A link handed out must have most of its life left: the person is
        # told it works for 15 minutes.
        if self.live_link is None or self.live_link[1] - now < LINK_SECONDS * 2 / 3:
            self.live_link = (secrets.token_urlsafe(32), now + LINK_SECONDS)
        return f"{self.url}?link={self.live_link[0]}"

    def _take_link(self, token):
        live = self.live_link
        if (
            not isinstance(token, str)
            or live is None
            or not hmac.compare_digest(token.encode(), live[0].encode())
        ):
            return False
        self.live_link = None
        return live[1] > self.clock()

    # -- visits ------------------------------------------------------------------

    def begin(self, request, label, continuation=None):
        """Start a visit for a person whose key or link was just accepted."""
        token = secrets.token_urlsafe(32)
        self.visit_digest = digest(token)
        self.visit = Visit(label, self.clock() + VISIT_SECONDS, continuation)
        self.last_seen = self.clock()
        self.save_error = None
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

    def _start_job(self, how, credentials=None, by=None):
        """Start a local login job.

        how: "save" (an email and password just typed here; saved once FBS
        opens), "saved" (the saved ones) or "live" (the person signs in on the
        live picture). Local: it may wait on Microsoft's pages for a person,
        because the person is right here.
        """
        permit = None
        if how in {"save", "saved"}:
            permit = self.service.sign_in_permit(
                credentials, saved=how == "saved", how="page", by=by
            )
        self.view = LiveView() if how == "live" else None
        self.frame = (0.0, None)
        self.save_error = None
        self.saved_unchecked = False
        # Until _finished has run, a succeeded "save" job is still saving: the
        # page must not say "saved" before it knows how it went.
        self.save_pending = how == "save"
        view_token = LIVE_VIEW.set(self.view)
        permit_token = SAVED_CREDENTIALS.set(permit)
        try:
            job = self.service.start("login")
        finally:
            SAVED_CREDENTIALS.reset(permit_token)
            LIVE_VIEW.reset(view_token)
        self.job_id, self.job_how = job["operation_id"], how
        self.job_save = (self.job_id, credentials, permit) if how == "save" else None
        task = self.service.jobs[self.job_id].task
        if task is not None and how == "save":
            # Saved even if the person closes the page meanwhile.
            task.add_done_callback(
                lambda _, job_id=self.job_id: self._finished(
                    job_id, credentials, permit
                )
            )
        if self.watchdog is None or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watch())

    def _finished(self, job_id, credentials, permit):
        """Save once, after the job succeeded: from its done-callback, or from
        the first status read that sees it succeeded, whichever comes first."""
        if self.saves_done.get(job_id):
            return
        try:
            job = self.service.jobs.get(job_id)
            store = self.store
            if job is None or job.status != "succeeded":
                return
            self.saves_done[job_id] = True
            if store is None:
                if job_id == self.job_id:
                    self.save_error = "saving is not available on this server now."
                return
            try:
                # Checked by Microsoft only if this job actually signed in with
                # them; with SMU still signed in, they are checked next time.
                store.save(
                    credentials.email, credentials.password, verified=permit.verified
                )
                if job_id == self.job_id:
                    self.saved_unchecked = not permit.verified
            except SavedSignInError as error:
                if job_id == self.job_id:
                    self.save_error = str(error)
        finally:
            if job_id == self.job_id:
                self.save_pending = False

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

    @staticmethod
    def _page_held(store):
        held = store.held()
        return bool(held and held.get("page_only"))

    def _saved(self):
        store = self.store
        if store is None:
            return {
                "can_save": False,
                "saved": False,
                "email": None,
                "owner": None,
                "recent": [],
            }
        summary = store.summary()
        return {
            "can_save": True,
            "saved": summary["saved"],
            "email": summary["email"],
            # The account this copy belongs to: the form is locked to it.
            "owner": summary["owner"],
            "recent": [
                {
                    "at": item.get("at"),
                    "how": "from chat" if item.get("how") == "chat" else "on this page",
                    "result": OUTCOMES.get(item.get("outcome"), "did not finish"),
                }
                for item in reversed(store.recent())
            ],
        }

    def _status(self, visit):
        job = self._job()
        live = self._live_page() is not None
        state = self.view.state if self.view is not None else None
        saved = self._saved()
        status = {
            "phase": "starting",
            "message": "Opening SMU sign-in…",
            "number": None,
            "picture": False,
            "field": "text",
            "retry": False,
            "next": None,
            # Show the form to save an email and password.
            "form": False,
            # Offer the live picture of Microsoft's page instead.
            "live": False,
            # Sign in with the saved ones without asking (an app is waiting).
            "auto": False,
            **saved,
        }
        if job is None:
            status.update(phase="idle", retry=True)
            if not saved["can_save"]:
                status["message"] = "Ready to sign in."
            elif (
                saved["saved"]
                and visit.label == "link"
                and self._page_held(self.store)
            ):
                status["form"] = True
                status["message"] = (
                    "Sagasu stopped signing in for you to protect your account. "
                    "Type your SMU password again to sign in."
                )
            elif saved["saved"]:
                status["message"] = (
                    f"Your SMU sign-in is saved for {saved['email']}."
                )
                status["auto"] = visit.continuation is not None
            else:
                status["form"] = True
                status["message"] = (
                    "Save your SMU sign-in on this copy of Sagasu. It signs in for "
                    "you with it, and you approve each sign-in in Microsoft "
                    "Authenticator."
                )
        elif job.status == "succeeded":
            if self.save_pending and self.job_save is not None:
                # Finished a moment ago and not saved yet: save now, so this
                # very answer says how it went.
                self._finished(*self.job_save)
                status.update(self._saved())
            status.update(phase="signed_in", message="You're signed in to SMU.")
            if self.job_how == "save":
                status["message"] = (
                    "Could not save your sign-in: " + self.save_error
                    if self.save_error
                    else (
                        "Saved. You were already signed in to SMU, so Microsoft "
                        "will check this password the next time Sagasu signs in. "
                        if self.saved_unchecked
                        else "Done. Your SMU sign-in is saved. "
                    )
                    + SAVED_HINT
                )
            if visit.continuation is not None and self.oauth is not None:
                if visit.next_url is None:
                    visit.next_url = self.oauth.finish(visit.continuation, visit.label)
                status["message"] += " Taking you back to the app…"
                status["next"] = visit.next_url
            elif self.job_how != "save":
                status["message"] = (
                    "You're signed in to SMU. Your agents can now use FBS as you. "
                    "You can close this page."
                )
            else:
                status["message"] += " You can close this page."
        elif job.status == "cancelled" or (self._ended(job) and not job.terminal):
            status.update(phase="stopped", message="Sign-in stopped.", retry=True)
            status["form"] = saved["can_save"] and not saved["saved"]
        elif job.terminal:
            reason = (job.error or {}).get("reason")
            status.update(
                phase="failed",
                message=FAILURES.get(reason, "Sign-in did not finish.")
                + (
                    ""
                    if reason in FAILURES and reason != "authentication_timeout"
                    else " Try again when you're ready. If SMU or Microsoft showed an"
                    " error or a block page, stop and check with SMU IT first."
                ),
                retry=True,
            )
            saved = self._saved()  # a refused password was just forgotten
            status.update(saved)
            status["form"] = saved["can_save"] and (
                not saved["saved"] or reason == "saved_password_rejected"
            )
            # Microsoft's own page, in the live picture: only for a visit
            # opened with the access key, never from a one-time link.
            status["live"] = (
                reason in {"sign_in_needs_page", "sign_in_unfinished"}
                and visit.label != "link"
            )
            if reason == "sign_in_needs_page" and visit.label == "link":
                status["message"] = (
                    "Microsoft asked for a step this page cannot do. Try again later, "
                    "or open this page with your access key to finish on "
                    "Microsoft's own page."
                )
        elif live:
            # The page itself is the best guide; an approval wait only says
            # more when the step is not otherwise known.
            message = STEPS.get(state) or (
                STEPS["authenticator"]
                if job.status == "awaiting_mfa"
                else "Follow the steps in the picture."
            )
            status.update(
                phase="signing_in",
                picture=True,
                message=message,
                field=FIELDS.get(state, "text"),
            )
        elif job.status == "awaiting_mfa" and self.job_how != "live":
            number = job.auth_number or (
                (found := NUMBER.search(job.auth_prompt or "")) and found.group(1)
            )
            status["phase"] = "approving"
            if number:
                status["number"] = number
                status["message"] = (
                    "Open Microsoft Authenticator on your phone and enter this "
                    "number to approve the sign-in:"
                )
            else:
                status["message"] = (
                    "Open Microsoft Authenticator on your phone and approve the "
                    "sign-in. If it asks for a number, it will appear here."
                )
        elif job.status == "queued":
            # Running jobs hold the queue; this one has not reached it yet.
            if self.service.queue.locked():
                status["message"] = "Waiting for the server to finish another request…"
        elif self.job_how in {"save", "saved"}:
            status["message"] = "Signing in to SMU…"
        else:
            status["message"] = "Checking your SMU sign-in…"
        return status

    # -- routes ------------------------------------------------------------------

    async def page(self, request: Request):
        if request.method == "POST":
            return await self._key_posted(request)
        link = request.query_params.get("link")
        if link is not None:
            # Confirm first: a link preview fetching the address must not use
            # the link up, and the visit cookie is set on this site's own post.
            return self._link_form(link)
        if self._visit(request) is None:
            return self._key_form()
        return HTMLResponse(self.view_page, headers=HEADERS)

    async def _key_posted(self, request):
        # Strict: a key posted from another site could open a sign-in to a copy
        # that is not the visitor's. This page's own post always names itself.
        if not self._same_site(request, strict=True):
            return self._key_form("This form can only be sent from its own page.", 403)
        form = await request.form()
        if "link" in form:
            job = self._job()
            if (
                self.visit is not None
                and self.visit.expires > self.clock()
                and (
                    self.visit.continuation is not None
                    or (job is not None and not self._ended(job))
                )
            ):
                # Never throw away an app's sign-in or one under way.
                return self._expired_link(
                    "A sign-in is already open on another page. Finish or cancel "
                    "it there first."
                )
            # A link opens only the page that saves this copy's own account.
            if self.store is None or not self._take_link(str(form.get("link") or "")):
                return self._expired_link()
            return self.begin(request, "link")
        address = self._address(request)
        if self.wrong_keys.used_up(address):
            return self._key_form(
                "Too many wrong keys. Wait ten minutes, then try again.", 429
            )
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
        body = KEY_BODY.format(error=_error(error))
        return HTMLResponse(
            PAGE.format(title="Sign in to SMU", body=body),
            status_code=status,
            headers=HEADERS,
        )

    def _expired_link(self, message=None):
        body = EXPIRED_BODY.format(
            message=html.escape(
                message
                or "This link has expired or was already used. Go back to your chat "
                "and ask Sagasu again; it will give you a new link."
            )
        )
        return HTMLResponse(
            PAGE.format(title="Sign in to SMU", body=body),
            status_code=400,
            headers=HEADERS,
        )

    def _link_form(self, link):
        body = LINK_BODY.format(
            host=html.escape(self.host), link=html.escape(link[:100], quote=True)
        )
        return HTMLResponse(
            PAGE.format(title="Sign in to SMU", body=body), headers=HEADERS
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
        visit, data = called
        job = self._job()
        if job is not None and not self._ended(job):
            # Join the sign-in already running.
            return JSONResponse(self._status(visit), headers=NO_STORE)
        store = self.store
        how = data.get("how")
        credentials = None
        if how is None:
            # Without a choice: the saved sign-in when there is one, else the
            # live picture where nothing can be saved (never for a link visit).
            if store is None and visit.label == "link":
                return JSONResponse({"error": "unavailable"}, 503, headers=NO_STORE)
            if store is None:
                how = "live"
            elif store.load() is not None:
                how = "saved"
            else:
                return JSONResponse({"error": "form"}, 400, headers=NO_STORE)
        if how == "save":
            email = data.get("email")
            password = data.get("password")
            email = email.strip() if isinstance(email, str) else email
            if store is None or not valid_password(password):
                return JSONResponse({"error": "bad_request"}, 400, headers=NO_STORE)
            try:
                # Only this copy's own SMU account, before anything is typed.
                store.check_account(email)
            except SavedSignInError as error:
                return JSONResponse(
                    {"error": "account", "message": str(error)}, 400, headers=NO_STORE
                )
            credentials = Credentials(email, password)
        elif how == "saved":
            credentials = store.load() if store is not None else None
            if credentials is None:
                return JSONResponse({"error": "form"}, 400, headers=NO_STORE)
            if visit.label == "link" and self._page_held(store):
                # After a hold only the page may lift, a key-less link may not
                # retype the saved password: the person types it again.
                return JSONResponse(
                    {
                        "error": "form",
                        "message": "Type your password again to sign in.",
                    },
                    400,
                    headers=NO_STORE,
                )
        elif how != "live" or visit.label == "link":
            # The live picture accepts any account, so only a visit opened
            # with the access key may use it, never one from a key-less link.
            return JSONResponse({"error": "bad_request"}, 400, headers=NO_STORE)
        if how != "live" and (limit := self.service.sign_in_limit()) is not None:
            return JSONResponse(
                {"error": "too_many", "retry_after_minutes": limit[1]},
                429,
                headers=NO_STORE,
            )
        from .mcp_service import AgentError

        try:
            self._start_job(how, credentials, by=visit.label)
        except AgentError:
            return JSONResponse({"error": "busy"}, 503, headers=NO_STORE)
        visit.next_url = None
        event("signin_desk.started", how=how)
        return JSONResponse(self._status(visit), headers=NO_STORE)

    async def forget(self, request: Request):
        called, refusal = await self._json_call(request)
        if refusal is not None:
            return refusal
        visit, _ = called
        store = self.store
        if store is not None:
            job = self._job()
            if job is not None and not self._ended(job) and self.job_how != "live":
                job.task.cancel()  # never finish a sign-in with what was deleted
            try:
                # The password goes; whose copy this is stays.
                store.forget_password()
            except SavedSignInError:
                return JSONResponse({"error": "failed"}, 500, headers=NO_STORE)
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
            action = type_into(page, text)
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
            Route(DESK_PATH + "/forget", endpoint=self.forget, methods=["POST"]),
            Route(DESK_PATH + "/status", endpoint=self.status, methods=["GET"]),
            Route(DESK_PATH + "/frame", endpoint=self.picture, methods=["GET"]),
            Route(DESK_PATH + "/input", endpoint=self.input, methods=["POST"]),
            Route(DESK_PATH + "/cancel", endpoint=self.cancel, methods=["POST"]),
            Route(DESK_PATH + "/view.js", endpoint=self.script, methods=["GET"]),
        ]


def _error(error):
    return f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""


# Text goes to the box that has focus. When a tap elsewhere left none, it goes
# to the page's one visible text box (never a guess between several).
FOCUS_ONLY_BOX = """() => {
  const active = document.activeElement;
  if (active && ['INPUT', 'TEXTAREA'].includes(active.tagName)) return;
  const boxes = [...document.querySelectorAll('input')].filter((box) =>
    ['text', 'email', 'password', 'tel', 'number'].includes(box.type) &&
    !box.disabled && !box.readOnly && box.offsetParent !== null &&
    getComputedStyle(box).opacity !== '0' && getComputedStyle(box).visibility !== 'hidden');
  if (boxes.length === 1) boxes[0].focus();
}"""


async def type_into(page, text):
    await page.evaluate(FOCUS_ONLY_BOX)
    await page.keyboard.type(text, delay=10)


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

EXPIRED_BODY = """<h1>Sign in to SMU</h1>
<p>{message}</p>"""

LINK_BODY = """<h1>Sign in to SMU</h1>
<p>This link opens the SMU sign-in for the Sagasu copy at <strong>{host}</strong>.</p>
<p class="muted">Only continue if your own agent gave you this link just now. The link works once.</p>
<form method="post" autocomplete="off">
<input type="hidden" name="link" value="{link}">
<button type="submit">Continue</button>
</form>"""

VIEW_BODY = """<h1>Sign in to SMU</h1>
<p class="muted">For the Sagasu copy at <strong>@HOST@</strong>. Only sign in here if that is your own copy.</p>
<p id="message" role="status" aria-live="polite">One moment…</p>
<p id="number" class="number" hidden aria-live="polite"></p>
<form id="setup" autocomplete="on" hidden>
<label for="email">SMU email</label>
<input id="email" name="username" type="email" required autocomplete="username" autocapitalize="off" autocorrect="off" spellcheck="false" inputmode="email">
<label for="password">SMU password</label>
<input id="password" name="password" type="password" required autocomplete="current-password" autocapitalize="off" autocorrect="off" spellcheck="false">
<p class="note">Sagasu keeps them encrypted on this copy, signs in to SMU with them when needed, and never shows them to an agent or a chat. You still approve every sign-in in Microsoft Authenticator. You can delete them here at any time.</p>
<button type="submit" id="save">Save and sign in</button>
</form>
<div id="saved" hidden>
<button type="button" id="signin">Sign in now</button>
<button type="button" id="replace" class="secondary">Save a different password</button>
<button type="button" id="forget" class="secondary">Delete my saved password</button>
</div>
<div id="history" hidden>
<p class="note"><strong>Recent sign-ins</strong></p>
<ul id="recent" class="note"></ul>
</div>
<button type="button" id="live" class="secondary" hidden>Finish on Microsoft's page</button>
<div id="livebox" hidden>
<form id="typing" autocomplete="off">
<div class="row">
<input id="text" type="text" aria-label="Type here" placeholder="Type here" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
<button type="submit" id="send" aria-label="Type this and press Enter">Send</button>
<button type="button" id="erase" class="secondary small" aria-label="Delete one character">⌫</button>
</div>
<p id="note" class="note" aria-live="polite"></p>
</form>
<div id="screen" class="screen" tabindex="0" aria-label="Microsoft sign-in page. Click or tap to press; on a computer you can also type here.">
<img id="picture" alt="" draggable="false">
</div>
<p class="muted">This is a live picture of Microsoft's sign-in page, running in the browser on your Sagasu server. What you type goes only to that page and is not kept.</p>
</div>
<p id="waiting" class="waiting">One moment…</p>
<button type="button" id="cancel" class="secondary" hidden>Cancel</button>"""

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
.note { color: var(--muted); font-size: 15px; margin: 8px 0 0; min-height: 1.5em; }
.number { font-size: 56px; font-weight: 700; letter-spacing: 6px; text-align: center; margin: 8px 0 18px; }
#message { font-weight: 600; }
"""

VIEW_SCRIPT = r"""'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const message = $('message'), number = $('number'), setup = $('setup'),
    email = $('email'), password = $('password'), save = $('save'),
    saved = $('saved'), signin = $('signin'), replace = $('replace'), forget = $('forget'),
    liveButton = $('live'), livebox = $('livebox'), typing = $('typing'), text = $('text'),
    screen = $('screen'), picture = $('picture'), waiting = $('waiting'),
    cancel = $('cancel'), erase = $('erase'), note = $('note'),
    history = $('history'), recent = $('recent');
  const EXPIRED = 'This page has expired. Go back to your chat and ask for a new link.';
  // A single-line field never holds a line break; a pasted one is dropped.
  const clean = (value) => value.replace(/[\r\n]+/g, '').slice(0, 256);
  let polling = false, framing = false, wanted = false, finished = false;
  let replacing = false, autoTried = false, last = null;
  let chain = Promise.resolve(), pending = '', flushTimer = null, shown = null;

  const post = (path, body) => fetch(path, {
    method: 'POST', cache: 'no-store', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}),
  });
  const send = (body) => { chain = chain.then(() => post('/sign-in/input', body)).catch(() => {}); return chain; };
  const busy = (status) => ['starting', 'approving', 'signing_in'].includes(status.phase);

  function render(status) {
    last = status;
    message.textContent = status.message;
    number.hidden = !status.number;
    number.textContent = status.number || '';
    wanted = !!status.picture;
    livebox.hidden = !wanted;
    const idle = !busy(status) && status.phase !== 'signed_in';
    setup.hidden = !(idle && status.can_save && (status.form || replacing));
    saved.hidden = !(idle && status.saved && setup.hidden);
    liveButton.hidden = !(idle && status.live);
    waiting.hidden = !(busy(status) && !wanted && !status.number);
    cancel.hidden = !busy(status);
    save.disabled = false;
    email.placeholder = 'name@smu.edu.sg';
    if (status.owner) {
      // This copy belongs to one SMU account; only that one can be saved.
      email.value = status.owner;
      email.readOnly = true;
    } else {
      email.readOnly = false;
    }
    recent.textContent = '';
    for (const item of status.recent || []) {
      const line = document.createElement('li');
      const when = item.at ? new Date(item.at * 1000).toLocaleString() : '';
      line.textContent = when + ': ' + item.result + ' (' + item.how + ')';
      recent.appendChild(line);
    }
    history.hidden = !(idle && (status.recent || []).length);
    // Changing the box's kind while someone types can make a phone keyboard
    // drop or repeat characters, so wait until the box is empty or left.
    if (text.type !== status.field && (document.activeElement !== text || !text.value)) {
      text.type = status.field;
    }
    if (wanted && !framing) frames();
    if (status.auto && !autoTried && status.phase === 'idle') { autoTried = true; start({how: 'saved'}); }
  }

  function stop(words) {
    finished = true; wanted = false;
    for (const element of [setup, saved, liveButton, livebox, waiting, cancel, number, history]) element.hidden = true;
    if (words) message.textContent = words;
  }

  async function poll() {
    if (polling) return;
    polling = true;
    while (!finished) {
      try {
        const response = await fetch('/sign-in/status', {cache: 'no-store', credentials: 'same-origin'});
        if (response.status === 401) { stop(EXPIRED); break; }
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

  async function start(body) {
    replacing = false;
    setup.hidden = saved.hidden = liveButton.hidden = true;
    waiting.hidden = false;
    message.textContent = 'Signing in to SMU…';
    try {
      const response = await post('/sign-in/start', body);
      if (response.status === 401) { stop(EXPIRED); return; }
      if (response.status === 429) {
        const answer = await response.json().catch(() => ({}));
        message.textContent = 'Too many sign-in attempts recently. Try again in ' +
          (answer.retry_after_minutes || 60) + ' minutes.';
      } else if (response.status === 400) {
        const answer = await response.json().catch(() => ({}));
        message.textContent = answer.message || 'Check the email and password, then try again.';
      }
      else if (!response.ok) message.textContent = 'The server is busy. Try again in a minute.';
      else render(await response.json());
      if (!response.ok && last) { setup.hidden = !last.can_save; waiting.hidden = true; }
    } catch (error) {
      message.textContent = 'Connection problem. Check your connection and try again.';
    }
    poll();
  }

  setup.addEventListener('submit', (event) => {
    event.preventDefault();
    const address = email.value.trim(), secret = password.value;
    if (!address || !secret) return;
    save.disabled = true;
    password.value = '';
    start({how: 'save', email: address, password: secret});
  });
  signin.addEventListener('click', () => start({how: 'saved'}));
  replace.addEventListener('click', () => { replacing = true; if (last) render(last); email.focus(); });
  forget.addEventListener('click', async () => {
    try {
      const response = await post('/sign-in/forget');
      if (response.ok) { replacing = false; render(await response.json()); message.textContent = 'Your saved sign-in is deleted.'; }
    } catch (error) { message.textContent = 'Connection problem. Try again.'; }
  });
  liveButton.addEventListener('click', () => start({how: 'live'}));

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
    const pasted = clean((event.clipboardData && event.clipboardData.getData('text')) || '');
    if (pasted) { event.preventDefault(); flush(); send({kind: 'text', text: pasted}); }
  });
  typing.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = clean(text.value);
    text.value = '';
    note.textContent = 'Sending…';
    chain = chain.then(async () => {
      if (value) {
        const typed = await post('/sign-in/input', {kind: 'text', text: value});
        if (!typed.ok) {
          // Nothing was typed, so do not submit Microsoft's form either.
          note.textContent = 'That could not be typed into the page. Check it and try again.';
          return;
        }
      }
      await post('/sign-in/input', {kind: 'key', key: 'Enter'});
      // The count, never the text: enough to spot a dropped or doubled letter.
      note.textContent = value
        ? 'Typed ' + value.length + ' character' + (value.length === 1 ? '' : 's') + ' and pressed Enter.'
        : 'Pressed Enter.';
    }).catch(() => { note.textContent = 'Connection problem. Try again.'; });
  });
  erase.addEventListener('click', () => send({kind: 'key', key: 'Backspace'}));
  cancel.addEventListener('click', async () => {
    try {
      const response = await post('/sign-in/cancel');
      const answer = response.ok ? await response.json() : {};
      stop('Sign-in cancelled.');
      if (answer.next) location.replace(answer.next);
    } catch (error) { stop('Sign-in cancelled.'); }
  });

  // Where nothing can be saved, sign in on the live picture straight away.
  fetch('/sign-in/status', {cache: 'no-store', credentials: 'same-origin'})
    .then((response) => response.status === 401 ? null : response.json())
    .then((status) => {
      if (!status) { stop(EXPIRED); return; }
      render(status);
      if (status.phase === 'idle' && !status.can_save) start({how: 'live'});
      else poll();
    })
    .catch(() => { message.textContent = 'Connection problem. Reload the page to try again.'; });
})();
"""

VIEW_PAGE = (
    PAGE.format(title="Sign in to SMU", body=VIEW_BODY)
    .replace("</style>", VIEW_STYLE + "</style>", 1)
    .replace('<div class="card">', '<div class="card desk">', 1)
    .replace("</main>", '</main>\n<script src="/sign-in/view.js"></script>', 1)
)
