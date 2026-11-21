"""Optional HTTP hosting adapters over Sagasu's shared implementation."""


def require_dependencies():
    from ..config import ConfigError

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ConfigError(
            "Hosting requires the hosted dependencies. Install with "
            "pip install 'sagasu[hosted]' (or pip install '.[hosted]' from the source tree)."
        ) from None
