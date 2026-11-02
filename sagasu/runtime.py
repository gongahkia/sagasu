"""A kernel lock keeps run/login/doctor from sharing a live browser profile."""

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, profile):
        self.path = Path(profile) / ".sagasu-sagasu.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise AlreadyRunning(
                "Another Sagasu process is using this browser profile. Stop it before starting mcp, login, or a session check. Close the other client first."
            ) from None
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
            self.stream = None
