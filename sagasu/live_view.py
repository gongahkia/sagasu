"""Compatibility entry point for :mod:`sagasu.hosted.live_view`."""

import sys

from .hosted import live_view as _implementation

sys.modules[__name__] = _implementation
