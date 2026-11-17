"""Where a call entered Sagasu: the private local socket/stdio, or the remote listener.

Remote callers hold only an API key. They may never start, steer or observe a
Microsoft sign-in, so the browser client and the job runner consult this value
instead of trusting each call site to pass a flag.
"""

import contextvars

CALL_ORIGIN = contextvars.ContextVar("sagasu_call_origin", default="local")


def is_remote():
    return CALL_ORIGIN.get() == "remote"
