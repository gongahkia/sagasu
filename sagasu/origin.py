"""Where a call entered Sagasu: the private local socket/stdio, or the remote listener.

Remote callers hold only an API key. They may never start, steer or observe a
Microsoft sign-in, so the browser client and the job runner consult this value
instead of trusting each call site to pass a flag.
"""

import contextvars

CALL_ORIGIN = contextvars.ContextVar("sagasu_call_origin", default="local")
# The label of the API key behind a remote call (never the key), so a sign-in
# it starts can be traced to the key or app that asked for it.
CALL_LABEL = contextvars.ContextVar("sagasu_call_label", default=None)


def is_remote():
    return CALL_ORIGIN.get() == "remote"


def caller():
    return CALL_LABEL.get() if is_remote() else "host"
