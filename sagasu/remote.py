"""Compatibility entry point for :mod:`sagasu.hosted.remote`."""

import sys

from .hosted import remote as _implementation

sys.modules[__name__] = _implementation
