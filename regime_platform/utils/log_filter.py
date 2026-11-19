"""
Sensitive data filtering for logging.

Filters out sensitive information like API keys, tokens, passwords, and other
PII from log messages to prevent information leakage.
"""

import re
import logging
from typing import Any, Dict, Optional

SENSITIVE_PATTERNS = [
    # API keys (nt_live_* and other formats)
    r'nt_live_[a-zA-Z0-9]{20,}',
    r'api[_-]?key[\'"=:\s]*[a-zA-Z0-9]{20,}',
    r'secret[_-]?key[\'"=:\s]*[a-zA-Z0-9]{20,}',
    r'token[\'"=:\s]*[a-zA-Z0-9_-]{20,}',  # JWT tokens
    
    # Database credentials
    r'postgresql://[^:]+:[^@]+@',
    r'mongodb://[^:]+:[^@]+@',
    r'redis://[^:]+:[^@]+@',
    r'password[\'"=:\s]*[^\s\'"<>]+',
    
    # Authorization headers
    r'authorization[\'"=:\s]*bearer\s+[a-zA-Z0-9_-]{20,}',
    r'authorization[\'"=:\s]*[a-zA-Z0-9]{20,}',
    
    # Sensitive numbers
    r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b',  # Credit card numbers
    r'\b\d{3}-\d{2}-\d{4}\b',  # SSN format
    
    # Email addresses (in certain contexts)
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
    
    # IPv6 addresses (full)
    r'\[([0-9a-fA-F:]+:+[0-9a-fA-F]*)+\]',

    # Current credential shapes (2026-11-18 security fix, scan finding 12):
    # Supabase post-2026-10 keys, management PATs, provider tokens, webhooks.
    r'sb_secret_[A-Za-z0-9_\-]{20,}',
    r'sb_publishable_[A-Za-z0-9_\-]{20,}',
    r'sbp_[A-Za-z0-9_\-]{20,}',
    r'nvapi-[A-Za-z0-9_\-]{20,}',
    r'\b\d{8,12}:AA[A-Za-z0-9_\-]{30,}',  # Telegram bot token
    r'https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+',
    r'rediss://[^:\s]+:[^@\s]+@',  # redis TLS URL with embedded password
    r'dev key:\s*[A-Za-z0-9_\-]{20,}',  # generated dev-key warnings
    # API key carried in a URL query string (WS auth, scan finding 16) —
    # uvicorn.access logs the full path+query, so scrub it at record creation.
    r'[?&]api_key=[^&\s"\']+',
]

# Cheap pre-check (2026-11-18): the record factory runs on EVERY log record,
# so scanning all patterns each time slowed the whole app/test suite ~5x.
# A record that contains none of these triggers cannot match any pattern
# above, so it is skipped without formatting or regex work. Covers every
# pattern family: key/secret/token/password words, provider prefixes, URLs
# (://), authorization/bearer, email (@), IPv6/brackets ([), and 3+ digit
# runs (credit-card / SSN shapes).
_TRIGGER_SUBSTRINGS = (
    "key", "secret", "token", "password", "passwd", "sb_", "sbp", "nvapi",
    "nt_live", "webhook", "://", "dev key", ":aa", "authorization", "bearer",
    "@", "[",
)
_TRIGGER_RE = re.compile(r"\d{3}")


def _maybe_sensitive(record: logging.LogRecord) -> bool:
    msg = record.msg
    if isinstance(msg, str):
        low = msg.lower()
        if any(t in low for t in _TRIGGER_SUBSTRINGS) or _TRIGGER_RE.search(low):
            return True
    args = record.args
    if isinstance(args, dict):
        args = args.values()
    if args:
        for a in args:
            if isinstance(a, str):
                low = a.lower()
                if any(t in low for t in _TRIGGER_SUBSTRINGS) or _TRIGGER_RE.search(low):
                    return True
    return False


class SensitiveDataFilter(logging.Filter):
    """Filter sensitive data from log messages."""
    
    def __init__(self, patterns: Optional[list] = None):
        self.patterns = patterns or SENSITIVE_PATTERNS
        self.compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.patterns]
        super().__init__()
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Filter sensitive data from log message.

        When args are present, render the message FIRST and scrub the
        rendered string — scrubbing the raw format string can destroy %-
        placeholders (e.g. '?api_key=%s' matches the query-key pattern) and
        break getMessage() with 'not all arguments converted'.
        """
        if not _maybe_sensitive(record):
            return True
        try:
            if record.args:
                rendered = record.getMessage()
                record.msg = self._filter_message(rendered)
                record.args = ()
                return True
        except Exception:
            pass  # malformed %-formatting: fall through to independent scrub

        if hasattr(record, 'msg') and record.msg:
            record.msg = self._filter_message(record.msg)

        if hasattr(record, 'args') and record.args:
            # Filter string arguments
            filtered_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    filtered_args.append(self._filter_message(arg))
                elif isinstance(arg, dict):
                    filtered_args.append(self._filter_dict(arg))
                elif isinstance(arg, (list, tuple)):
                    filtered_args.append(self._filter_sequence(arg))
                else:
                    filtered_args.append(arg)
            record.args = tuple(filtered_args)

        return True
    
    def _filter_message(self, message: str) -> str:
        """Filter sensitive data from a string message."""
        if not isinstance(message, str):
            return message
        for pattern in self.compiled_patterns:
            message = pattern.sub('[REDACTED]', message)
        return message
    
    def _filter_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively filter sensitive data from a dictionary."""
        filtered = {}
        for key, value in data.items():
            if isinstance(value, str):
                filtered[key] = self._filter_message(value)
            elif isinstance(value, dict):
                filtered[key] = self._filter_dict(value)
            elif isinstance(value, (list, tuple)):
                filtered[key] = self._filter_sequence(value)
            else:
                filtered[key] = value
        return filtered
    
    def _filter_sequence(self, sequence) -> list:
        """Recursively filter sensitive data from sequences."""
        filtered = []
        for item in sequence:
            if isinstance(item, str):
                filtered.append(self._filter_message(item))
            elif isinstance(item, dict):
                filtered.append(self._filter_dict(item))
            elif isinstance(item, (list, tuple)):
                filtered.append(self._filter_sequence(item))
            else:
                filtered.append(item)
        return filtered

_FACTORY_INSTALLED = False


def configure_logging_filter():
    """Install sensitive-data scrubbing on EVERY log record.

    Logger-level filters only apply to records logged directly on that
    logger; records propagated from child loggers (the app's modules, and
    uvicorn.access) bypass them — the old root-logger attachment was a no-op
    for exactly the records that carry secrets (scan finding 12). A
    LogRecordFactory scrubs at record creation, covering every logger and
    handler regardless of when it is attached. Handler-level filters are
    added as well for defense in depth. Idempotent.
    """
    global _FACTORY_INSTALLED
    root_logger = logging.getLogger()
    filt = SensitiveDataFilter()

    for handler in root_logger.handlers:
        if not any(isinstance(f, SensitiveDataFilter) for f in handler.filters):
            handler.addFilter(filt)

    if _FACTORY_INSTALLED:
        return

    _orig_factory = logging.getLogRecordFactory()

    def _scrubbing_factory(*args, **kwargs):
        record = _orig_factory(*args, **kwargs)
        try:
            filt.filter(record)
        except Exception:
            pass  # scrubbing must never break logging
        return record

    logging.setLogRecordFactory(_scrubbing_factory)
    _FACTORY_INSTALLED = True