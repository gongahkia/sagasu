"""Per-operation authentication inputs shared by local and hosted adapters."""

import contextvars

SAVED_CREDENTIALS = contextvars.ContextVar("sagasu_saved_credentials", default=None)
LIVE_VIEW = contextvars.ContextVar("sagasu_live_view", default=None)


class Permit:
    """Leave for one job to sign in once with these credentials.

    The browser client takes it before typing anything; a second sign-in in the
    same job is refused, so no loop can send more than one Authenticator request.
    """

    def __init__(
        self, credentials, *, saved=True, on_start=None, on_password=None, on_end=None
    ):
        self.credentials = credentials
        self.saved = saved  # from the store, rather than a form just filled in
        self._on_start = on_start
        self._on_password = on_password
        self._on_end = on_end
        self.used = False
        self.password_typed = False
        self.approval_requested = False  # Microsoft showed the phone step
        self.signed_in = False  # FBS opened after this sign-in began
        self.outcome = None

    @property
    def verified(self):
        """Microsoft itself accepted this password: it was typed, and FBS
        opened. A sign-in that only resumed an existing session checks nothing."""
        return self.signed_in and self.password_typed

    def take(self):
        if self.used:
            return False
        self.used = True
        if self._on_start is not None:
            self._on_start()
        return True

    def finish(self, outcome):
        """How the sign-in ended: "signed_in" or one of the failure reasons."""
        if self.outcome is not None:
            return
        self.outcome = outcome
        self.signed_in = outcome == "signed_in"
        if self._on_end is not None:
            self._on_end(self, outcome)

    def typing_password(self):
        """Called just before the password is typed: counted first, so a crash
        in between still counts it."""
        self.password_typed = True
        if self._on_password is not None:
            self._on_password()

    def __repr__(self):
        return f"Permit({self.credentials!r}, used={self.used})"

