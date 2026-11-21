"""Compatibility entry point for :mod:`sagasu.hosted.signin_desk`."""

import sys

from .hosted import signin_desk as _implementation

sys.modules[__name__] = _implementation
