"""Saved sign-in authentication policy for hosted instances."""

from ..auth_context import Permit
from ..authentication import Authentication, HOLDS
from ..observability import event


class HostedAuthentication(Authentication):
    def __init__(self, store, *, clock):
        super().__init__(clock=clock)
        self.store = store

    def sign_in_permit(self, credentials, *, saved, how=None, by=None):
        """Leave for one job to sign in with these, counted in the ledger.

        how: "chat" or "page", and by: the key label, for the person's record
        of recent sign-ins.
        """
        store = self.store
        by = by or "unknown"
        how = how or "page"
        return Permit(
            credentials,
            saved=saved,
            on_start=(lambda: store.record("starts")) if store else None,
            on_password=(lambda: store.record("passwords")) if store else None,
            on_end=lambda permit, outcome: self._sign_in_ended(
                permit, outcome, by, how
            ),
        )

    def _sign_in_ended(self, permit, outcome, by, how):
        event("saved_sign_in.ended", outcome=outcome, key=by, how=how)
        store = self.store
        if store is None:
            return
        # Each step on its own: the hold matters most, so it comes first, and
        # a failure in one never skips the others.
        steps = []
        if outcome == "signed_in":
            if permit.saved and permit.verified:
                steps.append(store.mark_verified)
            steps.append(lambda: store.held() and store.lift(by_page=True))
        else:
            if outcome in HOLDS and permit.saved:
                # Only a failure of the saved sign-in holds chat. A password
                # mistyped on the page is the page's to show, and the saved
                # one still works.
                steps.append(lambda: store.hold(outcome, page_only=HOLDS[outcome]))
            if outcome == "saved_password_rejected" and permit.saved:
                # Never try a refused password again (lockouts).
                steps.append(lambda: store.forget_password(refused=True))
        steps.append(lambda: store.note(by, how, outcome))
        for step in steps:
            try:
                step()
            except Exception as error:
                event(
                    "saved_sign_in.record_failed",
                    level=40,
                    error_type=type(error).__name__,
                )

    def sign_in_limit(self):
        """(reason, minutes) when the limits allow no sign-in now, else None."""
        store = self.store
        if store is None:
            return None
        refused = store.refusal()
        if refused is None or refused[0] != "sign_in_attempts_exceeded":
            return None
        return refused

    def prepare(self, job, *, by):
        """(permit, None), or (None, (reason, minutes)) for a remote job.

        (None, None): this host does not sign in for remote callers at all;
        the person signs in on the host, as before.
        """
        store = self.store
        if store is None:
            return None, None
        if job.kind == "login":
            # The person asked: a hold set before they asked, that chat may
            # lift, is lifted once. A later one stays (it is their answer).
            store.lift(by_page=False, asked_at=job.created_at)
        refused = store.refusal()
        if refused is not None and refused[0] in HOLDS:
            # Before anything else: a rejected password is already deleted,
            # and the person must hear why, not that nothing is saved.
            return None, refused
        credentials = store.load()
        if credentials is None:
            return None, ("sign_in_setup_needed", None)
        if refused is not None:
            return None, refused
        return self.sign_in_permit(credentials, saved=True, by=by, how="chat"), None

    def signed_in(self):
        super().signed_in()
        if self.store is not None and self.store.held():
            self.store.lift(by_page=True)
