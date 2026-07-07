"""
Comprehensive input validation middleware for FastAPI.

Validates and sanitizes incoming requests to prevent injection attacks
and ensure data integrity across all endpoints.
"""

import re
import logging
from typing import Any, Dict, List, Optional, Union
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

class ValidationError(HTTPException):
    """Custom validation error with structured details."""
    
    def __init__(self, detail: Union[str, Dict[str, Any]]):
        if isinstance(detail, str):
            detail = {"message": detail}
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": detail}
        )

class InputValidationMiddleware:
    """Middleware for comprehensive input validation."""
    
    def __init__(self, exempt_paths: Optional[List[str]] = None):
        self.exempt_paths = exempt_paths or [
            "/docs",
            "/redoc",
            "/openapi.json",
            "/health",
            "/metrics",
        ]
        
        # Common patterns to block
        self.blocked_patterns = [
            r'<script[^>]*>.*?</script>',  # Script tags
            r'javascript:',               # JavaScript protocol
            r'on\w+\s*=',                # Event handlers
            r'data:text/html',           # Data URIs
            r'vbscript:',                # VBScript protocol
            r'eval\s*\(',                # eval() calls
            r'document\.',               # DOM access
            r'window\.',                # Window object
            r'<iframe',                  # Iframes
            r'<object',                  # Objects
            r'<embed',                   # Embeds
            r'<frame',                   # Frames
            r'<link[^>]*rel=[\'"]stylesheet[\'"]',  # External stylesheets
        ]
    
    async def __call__(self, request: Request, call_next):
        # Skip validation for exempt paths
        if any(request.url.path.startswith(path) for path in self.exempt_paths):
            return await call_next(request)
        
        # Only validate certain HTTP methods
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                # Validate JSON body if present
                content_type = request.headers.get("content-type", "")
                if "application/json" in content_type:
                    await self._validate_json_body(request)
                
                # Validate query parameters
                await self._validate_query_params(request)
                
                # Validate path parameters
                await self._validate_path_params(request)
                
            except ValidationError as e:
                return JSONResponse(
                    status_code=e.status_code,
                    content=e.detail,
                )
        
        response = await call_next(request)
        return response
    
    async def _validate_json_body(self, request: Request):
        """Validate JSON request body."""
        try:
            body = await request.json()
            self._sanitize_data(body)
        except Exception as e:
            log.warning(f"Invalid JSON body: {e}")
            raise ValidationError("Invalid JSON format")
    
    async def _validate_query_params(self, request: Request):
        """Validate query parameters."""
        for key, value in request.query_params.items():
            self._validate_input_value(key, value, "query")
    
    async def _validate_path_params(self, request: Request):
        """Validate path parameters."""
        # Path params are already processed by FastAPI, but we can add additional validation
        if hasattr(request, 'path_params'):
            for key, value in request.path_params.items():
                if value:
                    self._validate_input_value(key, value, "path")
    
    def _sanitize_data(self, data: Any, path: str = ""):
        """Recursively sanitize and validate data."""
        if isinstance(data, dict):
            for key, value in data.items():
                current_path = f"{path}.{key}" if path else key
                self._validate_input_key(key, current_path)
                
                if isinstance(value, str):
                    self._validate_input_value(key, value, current_path)
                elif isinstance(value, (dict, list)):
                    self._sanitize_data(value, current_path)
        
        elif isinstance(data, list):
            for i, item in enumerate(data):
                current_path = f"{path}[{i}]" if path else f"[{i}]"
                if isinstance(item, str):
                    self._validate_input_value(f"item_{i}", item, current_path)
                elif isinstance(item, (dict, list)):
                    self._sanitize_data(item, current_path)
    
    def _validate_input_key(self, key: str, path: str):
        """Validate input field names."""
        # Block dangerous field names
        dangerous_keys = [
            'password', 'pwd', 'passwd', 'secret', 'token', 'key',
            'auth', 'credential', 'session', 'cookie', 'jwt',
            'sql', 'query', 'script', 'iframe', 'object', 'embed'
        ]
        
        if any(danger in key.lower() for danger in dangerous_keys):
            raise ValidationError(f"Invalid field name: {key}")
        
        # Block field names with special characters
        if re.search(r'[<>"\'\(\)\{\}\[\];]', key):
            raise ValidationError(f"Invalid characters in field name: {key}")
    
    def _validate_input_value(self, key: str, value: str, path: str):
        """Validate and sanitize input values."""
        # Check for XSS patterns
        for pattern in self.blocked_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                log.warning(f"Blocked XSS pattern in {path}: {pattern}")
                raise ValidationError(f"Invalid input in {path}")
        
        # Check for SQL injection patterns
        sql_patterns = [
            r'\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|EXEC|CREATE|ALTER|TRUNCATE)\s',
            r'\b(OR|AND)\s+\d+\s*=\s*\d+',
            r'\b(WHERE|HAVING)\s+.*[=<>]',
        ]
        
        for pattern in sql_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                log.warning(f"Blocked SQL injection pattern in {path}: {pattern}")
                raise ValidationError(f"Invalid input in {path}")
        
        # Sanitize HTML entities from string values
        if len(value) > 1000:  # Only sanitize long strings to avoid overhead
            value = re.sub(r'<[^>]+>', '', value)  # Remove HTML tags
            # Escape other potentially dangerous characters
            value = value.replace('\x00', '')  # Null bytes
            value = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', value)  # Control characters
    
    @staticmethod
    def create_validation_middleware(exempt_paths: Optional[List[str]] = None):
        """Factory function to create validation middleware."""
        middleware = InputValidationMiddleware(exempt_paths)
        
        async def validation_middleware(request: Request, call_next):
            return await middleware(request, call_next)
        
        return validation_middleware