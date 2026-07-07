# LightningAI Authentication Configuration Guide

## Overview
This guide explains how to configure authentication for your Noble Trader platform on LightningAI.

## Authentication Modes

### 1. API Key Only Mode (Recommended for LightningAI)
**Best for your push-based architecture**

```bash
# Set this in LightningAI environment variables
AUTH_MODE=api_key_only

# Configure your API keys
API_KEYS=your_lightningai_service_key

# Optional: Multiple API keys
API_KEYS=key1,key2,key3
```

**Behavior:**
- JWT tokens are completely ignored
- Only API key authentication works
- Fast and lightweight for internal service calls

### 2. Full Mode (Default)
**Multi-layer authentication**

```bash
# Multi-layer: JWT + API Keys + Clerk
AUTH_MODE=full

# Required for JWT
JWT_SECRET_KEY=your_jwt_secret

# API keys still work
API_KEYS=your_service_key
```

### 3. Disabled Mode (Development Only)
**No authentication - use with caution**

```bash
# Development only - NOT for production
AUTH_MODE=disabled
```

## Migration from Legacy Setup

### If you were using `AUTH_ENABLED=false`:

**Before:**
```bash
AUTH_ENABLED=false
```

**After (recommended):**
```bash
AUTH_MODE=api_key_only
API_KEYS=your_service_key
```

### If you want to keep JWT but disable Clerk:

```bash
AUTH_MODE=full
# JWT and API keys work, Clerk is ignored
```

## Environment Validation

The system automatically validates your configuration:

1. **Production/Staging blocks disabled mode**
2. **Warns if API keys not configured when auth is enabled**
3. **Validates JWT secret when JWT mode is active**

## LightningAI Specific Recommendations

### For Internal Service Calls
```bash
AUTH_MODE=api_key_only
API_KEYS=lightningai_worker_1,lightningai_worker_2
```

### For Development
```bash
AUTH_MODE=disabled
# or for semi-authenticated development
AUTH_MODE=api_key_only
API_KEYS=dev_key_1,dev_key_2
```

### For Production (when needed)
```bash
AUTH_MODE=full
JWT_SECRET_KEY=your_production_jwt_secret
API_KEYS=production_service_key
```

## API Key Format

### Static API Keys (Simple)
```
API_KEYS=my_service_key,another_key
```

### SaaS API Keys (Database-backed)
```
API_KEYS=nt_live_abc123def456
```

## Security Notes

- **API Key Only mode** is perfect for LightningAI's push-based architecture
- **No JWT overhead** = faster internal service calls
- **Easy to rotate** API keys without changing deployment
- **Backward compatible** with existing API key usage
- **Environment validation** prevents misconfiguration

## Testing Your Configuration

1. **Check current mode:**
   ```bash
   echo "Current auth mode: $AUTH_MODE"
   echo "API keys configured: ${API_KEYS:+yes}"
   ```

2. **Test endpoint access:**
   ```bash
   # With API key
   curl -H "X-API-Key: your_key" http://your-endpoint/health
   ```

3. **Test authentication mode:**
   ```bash
   # Without credentials (should fail in API key mode)
   curl http://your-endpoint/health
   ```

## Troubleshooting

### "Authentication required" errors:
- Check `AUTH_MODE` setting
- Verify `API_KEYS` are configured
- Ensure correct header format (`X-API-Key: your_key`)

### "Invalid API key" errors:
- Check key format (no extra spaces)
- Verify key exists in configuration
- Check for case sensitivity

### JWT-related errors in API Key mode:
- These are normal - JWT is ignored in `api_key_only` mode
- Only use `X-API-Key` header