"""Compatibility entry point for :mod:`sagasu.hosted.gateway`."""

import sys

from .hosted import gateway as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
