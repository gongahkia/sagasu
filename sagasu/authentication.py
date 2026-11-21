"""Authentication handling injected into the shared operation service."""

import time

from .auth_context import Permit
from .fbs_client import AuthenticationRequired, SAVED_SIGN_IN_REASONS

SIGN_IN_RETRY_SECONDS = 6 * 3600
HOLDS = {
    "saved_password_rejected": True,
    "sign_in_blocked": True,
    "sign_in_needs_page": True,
    "sign_in_not_approved": False,
    "sign_in_unfinished": False,
}


class Authentication:
    """Local authentication, also supporting remote access without saved credentials."""

    store = None

    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.retry_at = 0.0
        self.pause_reason = "sign_in_required_on_host"

    @property
    def host_required(self):
        return self.clock() < self.retry_at

    def sign_in_permit(self, credentials, *, saved, how=None, by=None):
        return Permit(credentials, saved=saved)

    def sign_in_limit(self):
        return None

    def prepare(self, job, *, by):
        return None, None

    def pause(self, reason):
        self.retry_at = self.clock() + SIGN_IN_RETRY_SECONDS
        self.pause_reason = reason

    def signed_in(self):
        self.retry_at = 0.0
        self.pause_reason = "sign_in_required_on_host"

    def failure(self, error, *, permit, refusal, backend_started):
        # Say who must act, and stop further remote requests from
        # reloading the sign-in page or sending more phone requests.
        if not backend_started:
            # Refused by a pause or hold itself: report it, never extend it.
            pass
        elif permit is not None and permit.used and not permit.signed_in:
            # This job signed in with the saved password, and it ended
            # short of FBS (recorded and held by the permit). Never
            # "retry once": that would type the password again.
            error = AuthenticationRequired(
                "SMU sign-in did not finish.",
                reason=(
                    permit.outcome
                    if permit.outcome in SAVED_SIGN_IN_REASONS
                    else "sign_in_unfinished"
                ),
            )
        elif error.reason == "sign_in_needed":
            # A check that must not sign in found the session ended.
            pass
        elif error.reason == "sign_in_required_on_host":
            # A person is needed and this job could not sign in:
            # nothing saved, or the limits stopped it.
            if refusal is not None:
                error = AuthenticationRequired(
                    "SMU sign-in is needed.",
                    reason=refusal[0],
                    details=(
                        {"retry_after_minutes": refusal[1]} if refusal[1] else None
                    ),
                )
            if error.reason in {
                "sign_in_required_on_host",
                "sign_in_setup_needed",
            }:
                self.pause(error.reason)
        elif refusal is not None:
            # Sign-in is on for this copy but this job could not use
            # it (nothing saved, or the limits), and the session did
            # not renew: say what the person can do, not "retry".
            error = AuthenticationRequired(
                "SMU sign-in is needed.",
                reason=refusal[0],
                details=({"retry_after_minutes": refusal[1]} if refusal[1] else None),
            )
        else:
            error = AuthenticationRequired(
                "The saved FBS session did not renew itself.",
                reason="session_renewal_pending",
            )
        return error
