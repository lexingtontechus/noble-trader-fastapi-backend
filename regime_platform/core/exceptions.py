"""Centralized exception -> HTTP status code mapping."""

import logging
from fastapi import HTTPException

log = logging.getLogger("regime.exceptions")


def classify_exception(exc: Exception) -> int:
    """Map an exception to the appropriate HTTP status code.

    Rules:
        ValueError, TypeError           -> 400  (bad input)
        KeyError, AttributeError        -> 400  (missing required field)
        numpy.linalg.LinAlgError        -> 422  (valid input, computation failed)
        scipy exceptions                -> 422  (valid input, computation failed)
        FileNotFoundError, LookupError   -> 404  (resource not found)
        ImportError, ModuleNotFoundError -> 503  (dependency missing)
        ConnectionError, TimeoutError,
        OSError                          -> 502  (upstream failure)
        Generic Exception                -> 500  (unexpected failure)
    """
    import numpy

    if isinstance(exc, (ValueError, TypeError)):
        return 400
    if isinstance(exc, (KeyError, AttributeError)):
        return 400
    if isinstance(exc, (numpy.linalg.LinAlgError,)):
        return 422  # Valid input, computation failed
    # Check for scipy exceptions if available
    try:
        import scipy
        if isinstance(exc, scipy.linalg.LinAlgError):
            return 422
    except (ImportError, AttributeError):
        pass
    if isinstance(exc, (FileNotFoundError, LookupError)):
        return 404
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return 503
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return 502
    # Default: unexpected error
    return 500


def raise_http_exception(exc: Exception, context: str = "") -> None:
    """Raise an HTTPException with the appropriate status code for the given exception.

    Usage:
        try:
            result = compute_something(data)
        except Exception as e:
            raise_http_exception(e, "regime detection")
    """
    status_code = classify_exception(exc)
    detail = f"{context}: {str(exc)}" if context else str(exc)
    log.error(f"HTTP {status_code}: {detail}")
    raise HTTPException(status_code=status_code, detail=detail)
