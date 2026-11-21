"""The sign-in page with a saved sign-in: one-time links, saving the person's
SMU email and password, the Authenticator number, forgetting, limits and the
record of recent sign-ins.

The routes are called in-process (tests.test_signin_desk.call). The saved
sign-in is a real SavedSignIn in a temporary directory. The login is a fake that
signs in through the SAVED_CREDENTIALS permit as the browser client does: take
it, type the password, show a number, finish. The page-script test drives
headless Chromium against a local server only. Never contacts FBS, Microsoft or
any outside service.
"""

from sagasu.hosted.authentication import HostedAuthentication

import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.applications import Starlette

from sagasu import errors
from sagasu.fbs_client import AuthenticationRequired
from sagasu.mcp_service import AgentService
from sagasu.saved_sign_in import (
    PASSWORDS_PER_DAY,
    SAVED_CREDENTIALS,
    SIGN_INS_PER_HOUR,
    Credentials,
    SavedSignIn,
    make_key_file,
)
from sagasu.signin_desk import LINK_SECONDS, SAVED_HINT, SignInDesk
from tests.test_remote import DIGEST
from tests.test_signin_desk import HOST, ORIGIN, DeskCase, call, settle

OWNER = "owner.person@smu.edu.sg"
OTHER = "someone.else@smu.edu.sg"
PASSWORD = "Fixture pass-word #1"
MASKED = "o…n@smu.edu.sg"
FORM = {"content-type": "application/x-www-form-urlencoded"}


class PermitBackend:
    """A login that signs in with the job's permit, as fbs_client does.

    session_valid: SMU is still signed in, so the permit is never used.
    """

    def __init__(self):
        self.gate = asyncio.Event()
        self.calls = []
        self.permits = []
        self.session_valid = False
        self.outcome = "signed_in"
        self.prompt = "Approve the sign-in request. Enter number 42."

    async def run(self, kind, payload, progress, notify):
        permit = SAVED_CREDENTIALS.get()
        self.calls.append(kind)
        self.permits.append(permit)
        await progress("Opening FBS")
        if permit is None or self.session_valid:
            await self.gate.wait()
            return {"status": "authenticated"}
        if not permit.take():
            raise AuthenticationRequired("Tried once.", reason="sign_in_unfinished")
        permit.typing_password()
        await notify(self.prompt)
        await self.gate.wait()
        permit.finish(self.outcome)
        if self.outcome != "signed_in":
            raise AuthenticationRequired("Not signed in.", reason=self.outcome)
        await progress("Signed in")
        return {"status": "authenticated"}


def cookie_of(headers):
    return headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]


class SavedDeskCase(DeskCase):
    account = None  # SAGASU_SMU_ACCOUNT

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir()
        self.key_file = make_key_file(self.root / "sign-in.key")
        self.store = SavedSignIn(
            self.root / "state", self.key_file, account=self.account
        )
        self.now = 1_000_000.0
        self.backend = PermitBackend()
        self.service = AgentService(backend=self.backend)
        self.service.authentication = HostedAuthentication(
            self.store, clock=self.service.clock
        )
        self.desk = SignInDesk(
            self.service, {DIGEST: "laptop"}, f"https://{HOST}", clock=lambda: self.now
        )
        self.app = Starlette(routes=self.desk.routes())
        self.addAsyncCleanup(self.cleanup)

    def token(self, link=None):
        return parse_qs(urlsplit(link or self.desk.link()).query)["link"][0]

    async def post_link(self, token, headers=ORIGIN):
        return await call(
            self.app,
            "POST",
            "/sign-in",
            headers={**FORM, **headers},
            body=urlencode({"link": token}).encode(),
        )

    async def link_visit(self):
        status, headers, body = await self.post_link(self.token())
        self.assertEqual(status, 303, body)
        return cookie_of(headers)

    async def start(self, cookie, expect=200, path="/sign-in/start", **body):
        status, _, raw = await call(
            self.app, "POST", path, headers=ORIGIN, body=body, cookie=cookie
        )
        self.assertEqual(status, expect, raw)
        self.assertNotIn(PASSWORD.encode(), raw)
        return json.loads(raw)

    async def forget(self, cookie):
        return await self.start(cookie, path="/sign-in/forget")

    async def settled(self):
        job = self.desk._job()
        await asyncio.gather(job.task, return_exceptions=True)
        return job

    def ledger(self):
        return json.loads(self.store.ledger_path.read_text())


class LinkTests(SavedDeskCase):
    async def test_errors_hand_out_one_link_until_it_is_used_or_expires(self):
        errors.set_sign_in_url(self.desk.url, self.desk.link)
        self.addCleanup(errors.set_sign_in_url, None)

        def linked(reason):
            failure = errors.public_failure(AuthenticationRequired("x", reason=reason))
            return failure.get("sign_in_url")

        first = linked("sign_in_setup_needed")
        self.assertTrue(first.startswith(f"https://{HOST}/sign-in?link="))
        # A later failure never replaces the link the person was just given.
        for reason in (
            "saved_password_rejected",
            "sign_in_needs_page",
            "sign_in_required_on_host",
            "session_renewal_pending",
        ):
            self.assertEqual(linked(reason), first, reason)
        # The page is also the way out where chat cannot retry.
        for reason in ("sign_in_not_approved", "sign_in_blocked", "sign_in_needed"):
            self.assertEqual(linked(reason), first, reason)
        self.assertIsNone(linked("authentication_timeout"))
        self.assertIn("sign_in_url", errors.sign_in_instruction())
        # Handed out with most of its 15 minutes left, never with seconds.
        self.now += LINK_SECONDS // 3 - 1
        self.assertEqual(self.desk.link(), first)
        self.now += 2  # under two thirds left: a new one
        second = linked("sign_in_setup_needed")
        self.assertNotEqual(second, first)
        self.assertEqual(self.desk.link(), second)
        status, _, _ = await self.post_link(self.token(second))
        self.assertEqual(status, 303)
        third = linked("sign_in_setup_needed")  # used: a new one
        self.assertNotIn(third, {first, second})

    async def test_opening_the_link_only_asks_to_continue(self):
        token = self.token()
        for _ in range(2):  # a link preview, then the person
            status, headers, body = await call(
                self.app, "GET", f"/sign-in?link={token}"
            )
            self.assertEqual(status, 200)
            self.assertIn(b'name="link"', body)
            self.assertIn(f'value="{token}"'.encode(), body)
            self.assertIn(f"<strong>{HOST}</strong>".encode(), body)
            self.assertIn(b"Continue", body)
            self.assertNotIn(b'name="key"', body)
            self.assertNotIn("set-cookie", headers)
            self.assertEqual(headers["cache-control"], "no-store")
        self.assertIsNone(self.desk.visit)
        self.assertEqual(self.service.jobs, {})
        self.assertEqual(self.token(), token)  # not used up
        _, _, body = await call(self.app, "GET", '/sign-in?link="><script>x</script>')
        self.assertNotIn(b"<script>x", body)

    async def test_continuing_uses_up_the_link_and_starts_a_visit(self):
        token = self.token()
        # Only this page may post it: not another site, a sandboxed frame, or
        # a post that does not say where it came from.
        for headers in ({"origin": "https://evil.example"}, {"origin": "null"}, {}):
            status, _, _ = await self.post_link(token, headers)
            self.assertEqual(status, 403, headers)
        self.assertIsNone(self.desk.visit)
        self.assertEqual(self.token(), token)
        status, headers, _ = await self.post_link(token)
        self.assertEqual((status, headers["location"]), (303, "/sign-in"))
        for part in ("HttpOnly", "Path=/sign-in", "SameSite=strict"):
            self.assertIn(part.lower(), headers["set-cookie"].lower())
        self.assertEqual(self.desk.visit.label, "link")
        cookie = cookie_of(headers)
        self.assertEqual((await self.status(cookie))["phase"], "idle")
        status, _, body = await call(self.app, "GET", "/sign-in", cookie=cookie)
        self.assertIn(b'src="/sign-in/view.js"', body)
        self.assertNotEqual(self.token(), token)
        self.assertEqual(self.service.jobs, {})

    async def test_a_used_wrong_or_expired_link_says_to_ask_the_chat_again(self):
        token = self.token()
        status, _, _ = await self.post_link(token)
        self.assertEqual(status, 303)
        live = self.token()
        for sent in (token, "w" * 43, live[:-1]):
            with self.subTest(sent=sent[:4]):
                status, headers, body = await self.post_link(sent)
                self.assertEqual(status, 400)
                self.assertNotIn(b'name="key"', body)
                self.assertNotIn(b'name="link"', body)
                self.assertIn(b"ask Sagasu again", body)
                self.assertIn(b"new link", body)
                self.assertNotIn("set-cookie", headers)
        # A wrong guess does not use up the person's real link.
        self.assertEqual(self.token(), live)
        self.now += LINK_SECONDS
        status, _, body = await self.post_link(live)
        self.assertEqual(status, 400)
        self.assertIn(b"ask Sagasu again", body)
        self.assertNotIn(b'name="key"', body)
        self.assertEqual(self.desk.visit.label, "link")  # the first visit stays

    async def test_a_link_never_replaces_a_running_sign_in(self):
        cookie = await self.visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        await self.until(cookie, "approving")
        token = self.token()
        status, headers, body = await self.post_link(token)
        self.assertEqual(status, 400)
        self.assertIn(b"already open", body)
        self.assertNotIn(b'name="key"', body)
        self.assertNotIn("set-cookie", headers)
        self.assertEqual(self.desk.visit.label, "laptop")
        self.assertEqual((await self.status(cookie))["phase"], "approving")
        self.assertEqual(self.token(), token)  # still unused
        self.backend.gate.set()
        await self.settled()
        status, _, _ = await self.post_link(token)
        self.assertEqual(status, 303)
        self.assertEqual(self.desk.visit.label, "link")

    async def test_a_link_never_replaces_an_apps_sign_in(self):
        continuation = {"client_id": "app", "params": None}
        self.desk.begin(types.SimpleNamespace(headers={}), "laptop", continuation)
        token = self.token()
        status, _, body = await self.post_link(token)
        self.assertEqual(status, 400)
        self.assertIn(b"already open", body)
        self.assertIs(self.desk.visit.continuation, continuation)
        self.assertEqual(self.token(), token)


class SaveTests(SavedDeskCase):
    async def test_with_nothing_saved_the_page_offers_the_form(self):
        cookie = await self.link_visit()
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "idle")
        self.assertTrue(state["form"])
        self.assertTrue(state["can_save"])
        self.assertEqual(
            (state["saved"], state["email"], state["owner"], state["recent"]),
            (False, None, None, []),
        )
        self.assertFalse(state["live"] or state["auto"])
        # Without a choice nothing starts: the person fills in the form.
        answer = await self.start(cookie, 400)
        self.assertEqual(answer["error"], "form")
        answer = await self.start(cookie, 400, how="saved")
        self.assertEqual(answer["error"], "form")
        answer = await self.start(cookie, 400, how="save", email=OWNER, password="")
        self.assertEqual(answer["error"], "bad_request")
        answer = await self.start(
            cookie, 400, how="save", email="me@gmail.com", password=PASSWORD
        )
        self.assertEqual(answer["error"], "account")
        self.assertIn("SMU email", answer["message"])
        self.assertEqual(self.service.jobs, {})

    async def test_a_save_that_signed_in_is_kept_as_verified(self):
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        state = await self.until(cookie, "approving")
        self.assertEqual(state["number"], "42")
        self.assertEqual(self.desk._job().auth_number, "42")
        self.assertIn("enter this number", state["message"])
        self.assertNotIn(PASSWORD, json.dumps(state))
        self.assertIsNone(self.store.load())  # nothing saved before Microsoft accepts
        self.backend.gate.set()
        await self.settled()
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "signed_in")
        self.assertIsNone(state["number"])
        self.assertTrue(state["message"].startswith("Done. Your SMU sign-in is saved."))
        self.assertIn(SAVED_HINT, state["message"])
        self.assertIn("close this page", state["message"])
        self.assertEqual(self.store.load(), Credentials(OWNER, PASSWORD))
        summary = self.store.summary()
        self.assertEqual((summary["verified"], summary["owner"]), (True, OWNER))
        permit = self.backend.permits[0]
        self.assertFalse(permit.saved)  # typed on the page, not from the store
        self.assertTrue(permit.signed_in)
        self.assertEqual((state["saved"], state["email"]), (True, MASKED))
        self.assertEqual(
            [(item["how"], item["result"]) for item in state["recent"]],
            [("on this page", "signed in")],
        )
        self.assertEqual(self.store.recent()[-1]["by"], "link")
        ledger = self.ledger()
        self.assertEqual((len(ledger["starts"]), len(ledger["passwords"])), (1, 1))

    async def test_a_save_while_still_signed_in_is_kept_unverified(self):
        self.backend.session_valid = True
        self.backend.gate.set()
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        await self.settled()
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "signed_in")
        self.assertIn("will check this password the next time", state["message"])
        self.assertIn(SAVED_HINT, state["message"])
        self.assertFalse(self.backend.permits[0].used)
        self.assertEqual(self.store.load(), Credentials(OWNER, PASSWORD))
        summary = self.store.summary()
        # Unchecked, so it does not yet claim the copy for this account.
        self.assertEqual((summary["verified"], summary["owner"]), (False, None))

    # Regression (fixed): the job is marked succeeded before its done-callback saves the
    # credentials and sets saved_unchecked, so a status read in between says
    # "Done. Your SMU sign-in is saved." (and saved: false) for an unchecked
    # save; the page script stops polling on that first signed_in status.
    async def test_the_first_signed_in_status_already_tells_how_it_was_saved(self):
        self.backend.session_valid = True
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        for _ in range(100):
            if self.desk._job().status == "running":
                break
            await asyncio.sleep(0.01)
        self.backend.gate.set()
        # The page's poll, arriving just as the sign-in finishes.
        state = await asyncio.create_task(self.status(cookie))
        self.assertEqual(state["phase"], "signed_in")
        self.assertIn("will check this password the next time", state["message"])
        self.assertTrue(state["saved"])

    async def test_without_a_number_the_page_says_it_will_appear(self):
        self.backend.prompt = "Approve the sign-in request in Microsoft Authenticator."
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        state = await self.until(cookie, "approving")
        self.assertIsNone(state["number"])
        self.assertIn("it will appear here", state["message"])

    async def test_a_refused_password_is_not_saved(self):
        self.backend.outcome = "saved_password_rejected"
        self.backend.gate.set()
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        await self.settled()
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "failed")
        self.assertIn("nothing was saved", state["message"])
        self.assertTrue(state["form"])
        self.assertIsNone(self.store.load())
        self.assertEqual(state["recent"][0]["result"], "password not accepted")

    async def test_a_verified_save_locks_the_copy_to_its_account(self):
        self.store.save(OWNER, PASSWORD, verified=True)
        cookie = await self.link_visit()
        state = await self.status(cookie)
        self.assertEqual((state["owner"], state["saved"]), (OWNER, True))
        self.assertFalse(state["form"])
        self.assertIn(MASKED, state["message"])
        answer = await self.start(
            cookie, 400, how="save", email=OTHER, password="Other 1"
        )
        self.assertEqual(answer["error"], "account")
        self.assertIn(MASKED, answer["message"])
        self.assertEqual(self.service.jobs, {})
        self.assertEqual(self.store.load(), Credentials(OWNER, PASSWORD))

    async def test_an_unchecked_save_does_not_lock_the_copy(self):
        self.store.save(OWNER, PASSWORD)
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OTHER, password="Other 1")
        self.backend.gate.set()
        await self.settled()
        self.assertEqual(self.store.load(), Credentials(OTHER, "Other 1"))


class OwnedSaveTests(SavedDeskCase):
    account = OWNER  # SAGASU_SMU_ACCOUNT

    async def test_another_account_is_refused_before_anything_runs(self):
        cookie = await self.link_visit()
        self.assertEqual((await self.status(cookie))["owner"], OWNER)
        answer = await self.start(
            cookie, 400, how="save", email=OTHER, password=PASSWORD
        )
        self.assertEqual(answer["error"], "account")
        self.assertIn(MASKED, answer["message"])
        self.assertNotIn(OTHER, answer["message"])
        self.assertEqual((self.service.jobs, self.backend.calls), ({}, []))
        self.assertFalse(self.store.path.exists())
        self.assertFalse(self.store.ledger_path.exists())  # no attempt counted
        # The owner's own account, however it is typed, is accepted.
        await self.start(
            cookie, how="save", email="  Owner.Person@SMU.edu.sg ", password=PASSWORD
        )
        self.backend.gate.set()
        await self.settled()
        self.assertEqual(self.store.load().password, PASSWORD)


class ForgetTests(SavedDeskCase):
    async def test_forget_deletes_the_password_but_keeps_the_owner(self):
        self.store.save(OWNER, PASSWORD, verified=True)
        cookie = await self.link_visit()
        state = await self.forget(cookie)
        self.assertEqual((state["saved"], state["owner"]), (False, OWNER))
        self.assertTrue(state["form"])
        self.assertIsNone(self.store.load())
        self.assertNotIn(PASSWORD.encode(), self.store.path.read_bytes())
        answer = await self.start(
            cookie, 400, how="save", email=OTHER, password=PASSWORD
        )
        self.assertEqual(answer["error"], "account")
        answer = await self.start(cookie, 400, how="saved")
        self.assertEqual(answer["error"], "form")
        self.assertEqual(self.service.jobs, {})

    async def test_forget_cancels_a_running_saved_sign_in(self):
        self.store.save(OWNER, PASSWORD, verified=True)
        cookie = await self.link_visit()
        await self.start(cookie)  # no choice: the saved ones
        await self.until(cookie, "approving")
        permit = self.backend.permits[0]
        self.assertTrue(permit.saved)
        await self.forget(cookie)
        job = await self.settled()
        self.assertEqual(job.status, "cancelled")
        self.assertIsNone(permit.outcome)  # never finished with what was deleted
        self.assertIsNone(self.store.load())
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "stopped")
        self.assertTrue(state["form"])

    async def test_forget_during_a_save_leaves_nothing_saved(self):
        cookie = await self.link_visit()
        await self.start(cookie, how="save", email=OWNER, password=PASSWORD)
        await self.until(cookie, "approving")
        await self.forget(cookie)
        job = await self.settled()
        await asyncio.sleep(0)
        self.assertEqual(job.status, "cancelled")
        self.assertIsNone(self.store.load())

    async def test_forget_leaves_a_live_sign_in_running(self):
        cookie = await self.visit()
        await self.start(cookie, how="live")
        await self.forget(cookie)
        await asyncio.sleep(0.05)
        self.assertFalse(self.desk._ended(self.desk._job()))
        self.assertEqual(self.backend.permits, [None])


class LiveAndLimitTests(SavedDeskCase):
    async def test_a_link_visit_never_gets_the_live_picture(self):
        cookie = await self.link_visit()
        for how in ("live", "magic"):
            answer = await self.start(cookie, 400, how=how)
            self.assertEqual(answer["error"], "bad_request")
        self.assertEqual(self.service.jobs, {})
        # With the access key, the live picture is allowed.
        key = await self.visit()
        await self.start(key, how="live")
        self.assertEqual(self.desk.job_how, "live")

    # Regression (fixed): start() refuses the live picture to a link visit only while the
    # store is available ("visit.label == 'link' and store is not None"), and
    # desk.link() keeps issuing links regardless; if the key file becomes
    # unreadable after start-up, a key-less link visit gets the any-account
    # live picture (and the page script starts it by itself).
    async def test_a_link_visit_never_gets_the_live_picture_without_a_store(self):
        cookie = await self.link_visit()
        self.key_file.unlink()  # the key is gone after start-up
        self.assertIsNone(self.desk.store)
        status, _, _ = await call(
            self.app,
            "POST",
            "/sign-in/start",
            headers=ORIGIN,
            body={"how": "live"},
            cookie=cookie,
        )
        self.assertNotEqual(status, 200)
        self.assertEqual(self.service.jobs, {})

    async def test_a_link_visit_is_not_offered_microsofts_page(self):
        self.store.save(OWNER, PASSWORD, verified=True)
        self.backend.outcome = "sign_in_needs_page"
        self.backend.gate.set()
        cookie = await self.link_visit()
        await self.start(cookie, how="saved")
        await self.settled()
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "failed")
        self.assertFalse(state["live"])
        self.assertIn("access key", state["message"])
        key = await self.visit()
        self.assertTrue((await self.status(key))["live"])

    async def test_used_up_limits_refuse_with_the_wait(self):
        self.store.save(OWNER, PASSWORD, verified=True)
        for _ in range(SIGN_INS_PER_HOUR):
            self.store.record("starts")
        cookie = await self.link_visit()
        for body in (
            {},
            {"how": "saved"},
            {"how": "save", "email": OWNER, "password": PASSWORD},
        ):
            with self.subTest(body=sorted(body)):
                answer = await self.start(cookie, 429, **body)
                self.assertEqual(answer["error"], "too_many")
                self.assertEqual(answer["retry_after_minutes"], self.store.refusal()[1])
                self.assertTrue(55 <= answer["retry_after_minutes"] <= 61)
        self.assertEqual(self.service.jobs, {})
        self.assertEqual(len(self.ledger()["starts"]), SIGN_INS_PER_HOUR)
        # Passwords typed count per day.
        self.store.forget_ledger()
        for _ in range(PASSWORDS_PER_DAY):
            self.store.record("passwords")
        answer = await self.start(cookie, 429, how="saved")
        self.assertGreater(answer["retry_after_minutes"], 60)
        # The live picture types nothing saved: a key visit may still use it.
        key = await self.visit()
        await self.start(key, how="live")
        self.assertEqual(len(self.service.jobs), 1)


class RecentTests(SavedDeskCase):
    async def test_recent_sign_ins_appear_after_outcomes(self):
        self.store.save(OWNER, PASSWORD)  # not yet checked
        self.store.note("laptop", "chat", "sign_in_not_approved")
        self.store.note("laptop", "chat", "something_else")
        cookie = await self.link_visit()
        self.assertEqual(
            [(i["how"], i["result"]) for i in (await self.status(cookie))["recent"]],
            [("from chat", "did not finish"), ("from chat", "not approved in time")],
        )
        # A saved sign-in that Microsoft accepts checks the saved password.
        self.backend.gate.set()
        await self.start(cookie, how="saved")
        await self.settled()
        self.assertTrue(self.store.summary()["verified"])
        self.assertEqual(self.store.summary()["owner"], OWNER)
        # One it refuses forgets the password and holds sign-ins from chat.
        self.backend.outcome = "saved_password_rejected"
        await self.start(cookie, how="saved")
        await self.settled()
        state = await self.status(cookie)
        self.assertEqual(state["phase"], "failed")
        self.assertFalse(state["saved"])
        self.assertTrue(state["form"])
        self.assertEqual(self.store.held()["reason"], "saved_password_rejected")
        self.assertEqual(
            [(i["how"], i["result"]) for i in state["recent"]][:3],
            [
                ("on this page", "password not accepted"),
                ("on this page", "signed in"),
                ("from chat", "did not finish"),
            ],
        )
        self.assertTrue(all(isinstance(i["at"], int) for i in state["recent"]))
        # Saving the new password on the page lifts the hold.
        self.backend.outcome = "signed_in"
        await self.start(cookie, how="save", email=OWNER, password="New pass 2")
        await self.settled()
        state = await self.status(cookie)
        self.assertEqual(state["recent"][0]["result"], "signed in")
        self.assertIsNone(self.store.held())
        self.assertEqual(self.store.load().password, "New pass 2")


class SavedPageScriptTests(SavedDeskCase):
    """The page's own script, in a real browser, as a person with a link uses it."""

    async def test_a_person_saves_their_sign_in_and_sees_the_number(self):
        import uvicorn
        from playwright.async_api import async_playwright

        from tests.test_remote import free_port

        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error")
        )
        serving = asyncio.create_task(server.serve())

        async def stop_server():
            server.should_exit = True
            await asyncio.wait_for(serving, 5)

        self.addAsyncCleanup(stop_server)
        while not server.started:
            await asyncio.sleep(0.02)
        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                self.skipTest("Install Playwright Chromium to run browser tests")
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    f"--host-resolver-rules=MAP {HOST} 127.0.0.1, MAP * ~NOTFOUND",
                    "--no-proxy-server",
                ],
            )
            try:
                person = await (await browser.new_context()).new_page()
                await person.goto(f"http://{HOST}:{port}/sign-in?link={self.token()}")
                self.assertEqual(await person.locator("#key").count(), 0)
                self.assertIsNone(self.desk.visit)
                await person.click("button[type=submit]")
                await person.wait_for_selector("#setup", state="visible")
                self.assertEqual(self.desk.visit.label, "link")
                self.assertTrue(await person.is_hidden("#live"))
                await person.fill("#email", OWNER)
                await person.fill("#password", PASSWORD)
                await person.click("#save")
                await settle(person, "#number", lambda text: text == "42")
                self.assertTrue(await person.is_visible("#number"))
                self.assertTrue(await person.is_hidden("#setup"))
                self.assertEqual(await person.input_value("#password"), "")
                self.assertIn(
                    "enter this number", await person.text_content("#message")
                )
                self.backend.gate.set()
                await settle(
                    person,
                    "#message",
                    lambda text: text.startswith("Done. Your SMU sign-in is saved."),
                )
                self.assertTrue(await person.is_hidden("#number"))
                self.assertEqual(self.store.load(), Credentials(OWNER, PASSWORD))
                self.assertTrue(self.store.summary()["verified"])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
