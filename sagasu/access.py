"""Caller access policy independent of any transport or hosted implementation."""

from dataclasses import dataclass

from .origin import caller, is_remote


@dataclass(frozen=True)
class Caller:
    remote: bool = False
    label: str | None = "host"


def current_caller():
    return Caller(remote=is_remote(), label=caller())


class AccessPolicy:
    def __init__(self, identity=current_caller):
        self.identity = identity

    @property
    def remote(self):
        return self.identity().remote

    @property
    def label(self):
        return self.identity().label

    def can_observe(self, remote):
        return not self.remote or remote is True

    @property
    def history_origin(self):
        return "remote" if self.remote else None
