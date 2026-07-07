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
]

class SensitiveDataFilter(logging.Filter):
    """Filter sensitive data from log messages."""
    
    def __init__(self, patterns: Optional[list] = None):
        self.patterns = patterns or SENSITIVE_PATTERNS
        self.compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.patterns]
        super().__init__()
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Filter sensitive data from log message."""
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

def configure_logging_filter():
    """Configure the root logger with sensitive data filtering."""
    # Get the root logger
    root_logger = logging.getLogger()
    
    # Add the filter if it doesn't exist
    for filter in root_logger.filters:
        if isinstance(filter, SensitiveDataFilter):
            return
    
    # Add our sensitive data filter
    root_logger.addFilter(SensitiveDataFilter())
    
    # Configure formatters to ensure consistent filtering
    for handler in root_logger.handlers:
        if hasattr(handler, 'formatter') and handler.formatter:
            # The filter will automatically process messages before formatting
            pass