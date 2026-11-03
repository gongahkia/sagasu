"""A kernel lock keeps run/login/doctor from sharing a live browser profile."""

import os
import json
import sys
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
            owner = ""
            try:
                self.stream.seek(0)
                metadata = json.loads(self.stream.read(1024))
                if type(metadata.get("pid")) is int:
                    owner = f" Owner PID: {metadata['pid']}."
            except (ValueError, OSError):
                pass
            self.stream.close()
            self.stream = None
            raise AlreadyRunning(
                "Another Sagasu process is using this browser profile."
                + owner
                + " Use sagasu host status to inspect the shared host, or close the older direct client before reconnecting."
            ) from None
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(
            json.dumps({"pid": os.getpid(), "program": Path(sys.argv[0]).name}).encode()
        )
        self.stream.flush()
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
            self.stream = None
