"""Compatibility entry point for :mod:`sagasu.hosted.oauth`."""

import sys

from .hosted import oauth as _implementation

sys.modules[__name__] = _implementation
