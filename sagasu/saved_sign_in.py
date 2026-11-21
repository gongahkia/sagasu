"""Compatibility entry point for :mod:`sagasu.hosted.saved_sign_in`."""

import sys

from .hosted import saved_sign_in as _implementation

sys.modules[__name__] = _implementation
