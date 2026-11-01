"""TTY-aware progress and redacted logging; structured data stays on stdout."""

import logging
import os
import re
import shutil
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ._vendor.rattles import LINE, PRESETS


class Reporter:
    def __init__(self, *, stream=None, spinner="dots", no_color=False, quiet=False):
        self.stream = stream or sys.stderr
        self.quiet = quiet
        tty = (
            self.stream.isatty()
            and os.environ.get("TERM") != "dumb"
            and not os.environ.get("CI")
        )
        self.animated = tty and spinner != "none" and not quiet
        self.color = tty and not no_color and "NO_COLOR" not in os.environ
        self.rattle = PRESETS.get(spinner, LINE)
        try:
            "".join(self.rattle.frames).encode(self.stream.encoding or "utf-8")
        except (UnicodeError, LookupError):
            self.rattle = LINE
        self.lock = threading.RLock()
        self.active = None

    def write(self, message, *, error=False):
        if self.quiet and not error:
            return
        with self.lock:
            if self.active and self.animated:
                self.stream.write("\r\033[2K")
            self.stream.write(str(message) + "\n")
            self.stream.flush()

    def check(self, name, status, message, hint=""):
        label = {"pass": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[status]
        if self.color:
            color = {"pass": "32", "warn": "33", "fail": "31", "skip": "90"}[status]
            label = f"\033[{color}m{label}\033[0m"
        self.write(f"[{label}] {name}: {message}", error=status in {"fail", "warn"})
        if hint:
            self.write(f"       {hint}", error=status in {"fail", "warn"})

    def progress(self, message):
        return Progress(self, message)


class Progress:
    def __init__(self, reporter, message):
        self.reporter = reporter
        self.message = message
        self.event = threading.Event()
        self.thread = None
        self.finished = False

    def __enter__(self):
        self.reporter.active = self
        if self.reporter.animated:
            self.thread = threading.Thread(target=self._render, daemon=True)
            self.thread.start()
        else:
            self.reporter.write(self.message)
        return self

    def _render(self):
        started = time.monotonic()
        while not self.event.is_set():
            with self.reporter.lock:
                width = max(10, shutil.get_terminal_size((80, 24)).columns - 2)
                text = f"{self.reporter.rattle.frame_at((time.monotonic()-started)*1000)} {self.message}"
                self.reporter.stream.write("\r\033[2K" + text[:width])
                self.reporter.stream.flush()
            self.event.wait(self.reporter.rattle.interval_ms / 1000)

    def finish(self, message=None, *, failed=False):
        if self.finished:
            return
        self.finished = True
        self.event.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.reporter.write(
            ("[FAIL] " if failed else "[OK] ") + (message or self.message), error=failed
        )
        self.reporter.active = None

    def __exit__(self, kind, value, traceback):
        if not self.finished:
            self.finish(
                "Operation interrupted." if kind else self.message,
                failed=kind is not None,
            )


class Redact(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = tuple(value for value in secrets if value)

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        message = re.sub(
            r"(?i)(https?://api\.telegram\.org/(?:file/)?bot)[^/\s]+",
            r"\1<redacted>",
            message,
        )
        message = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", message)
        record.msg, record.args = message, ()
        # Third-party tracebacks can embed token-bearing request URLs.
        if record.exc_info:
            record.msg += f" ({record.exc_info[0].__name__}; raw traceback suppressed)"
            record.exc_info = None
            record.exc_text = None
        record.stack_info = None
        return True


class ReporterHandler(logging.Handler):
    def __init__(self, reporter):
        super().__init__()
        self.reporter = reporter

    def emit(self, record):
        self.reporter.write(
            self.format(record), error=record.levelno >= logging.WARNING
        )


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(
            self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
        )
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(
            descriptor, self.mode, encoding=self.encoding, errors=self.errors
        )


def configure_logging(reporter, *, verbose=False, log_file=None, secrets=()):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "sagasu_handler", False):
            root.removeHandler(handler)
            handler.close()
    handlers = [ReporterHandler(reporter)]
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        handlers.append(
            PrivateRotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.sagasu_handler = True
        handler.addFilter(Redact(secrets))
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.WARNING)
