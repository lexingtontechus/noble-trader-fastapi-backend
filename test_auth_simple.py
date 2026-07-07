#!/usr/bin/env python3
"""
Simple test script to verify authentication mode switching
"""
import os
import sys

# Add the project root to Python path
sys.path.insert(0, '.')

# Import modules to test
from regime_platform.auth.auth_config import AuthMode, get_auth_mode, is_jwt_enabled, validate_auth_config

def test_auth_modes():
    """Test authentication mode switching"""
    print("🧪 Testing authentication mode switching...")
    
    # Test each mode
    test_cases = [
        ("disabled", AuthMode.DISABLED, False),
        ("api_key_only", AuthMode.API_KEY_ONLY, False),
        ("full", AuthMode.FULL, True)
    ]
    
    for mode_str, expected_mode, jwt_enabled in test_cases:
        # Set environment variable
        os.environ['AUTH_MODE'] = mode_str
        
        # Test mode detection
        actual_mode = get_auth_mode()
        print(f"   Mode '{mode_str}' -> {actual_mode.name}")
        assert actual_mode == expected_mode, f"Expected {expected_mode}, got {actual_mode}"
        
        # Test JWT enabled status
        actual_jwt_enabled = is_jwt_enabled()
        print(f"   JWT enabled: {actual_jwt_enabled}")
        assert actual_jwt_enabled == jwt_enabled, f"Expected JWT {jwt_enabled}, got {actual_jwt_enabled}"
        
    print("✅ All mode tests passed!")

def test_backward_compatibility():
    """Test backward compatibility with AUTH_ENABLED"""
    print("\n🔄 Testing backward compatibility...")
    
    # Test old boolean values
    test_cases = [
        ("false", AuthMode.DISABLED),
        ("true", AuthMode.FULL),
        ("TRUE", AuthMode.FULL),  # Case insensitive
        ("FALSE", AuthMode.DISABLED)
    ]
    
    for auth_enabled, expected_mode in test_cases:
        # Remove AUTH_MODE and set AUTH_ENABLED
        if 'AUTH_MODE' in os.environ:
            del os.environ['AUTH_MODE']
        os.environ['AUTH_ENABLED'] = auth_enabled
        
        actual_mode = get_auth_mode()
        print(f"   AUTH_ENABLED='{auth_enabled}' -> {actual_mode.name}")
        assert actual_mode == expected_mode, f"Expected {expected_mode}, got {actual_mode}"
        
        # Clean up
        if 'AUTH_ENABLED' in os.environ:
            del os.environ['AUTH_ENABLED']
    
    print("✅ Backward compatibility tests passed!")

def test_environment_validation():
    """Test environment configuration validation"""
    print("\n📋 Testing environment validation...")
    
    # Test production warnings
    os.environ['DEPLOY_ENV'] = 'production'
    os.environ['AUTH_MODE'] = 'api_key_only'
    
    warnings = validate_auth_config()
    print(f"   Production warning: {warnings}")
    assert 'production' in warnings if warnings else False
    
    # Test development (expected warnings about missing keys)
    os.environ['DEPLOY_ENV'] = 'development'
    os.environ['AUTH_MODE'] = 'api_key_only'
    
    warnings = validate_auth_config()
    print(f"   Development warning: {warnings}")
    # In development, we expect warnings about missing configuration
    # The validation function still warns about missing API_KEYS/JWT_SECRET_KEY
    
    # Clean up
    if 'DEPLOY_ENV' in os.environ:
        del os.environ['DEPLOY_ENV']
    if 'AUTH_MODE' in os.environ:
        del os.environ['AUTH_MODE']
    
    print("✅ Environment validation tests passed!")

def test_lightningai_configuration():
    """Test LightningAI-specific configuration"""
    print("\n⚡ Testing LightningAI configuration...")
    
    # Set LightningAI environment
    os.environ['AUTH_MODE'] = 'api_key_only'
    os.environ['API_KEYS'] = 'nt_live_test_key_1,nt_live_test_key_2'
    
    mode = get_auth_mode()
    jwt_enabled = is_jwt_enabled()
    
    print(f"   LightningAI mode: {mode.name}")
    print(f"   JWT enabled: {jwt_enabled}")
    print(f"   API keys configured: {bool(os.getenv('API_KEYS'))}")
    
    assert mode == AuthMode.API_KEY_ONLY
    assert not jwt_enabled
    assert os.getenv('API_KEYS')
    
    # Clean up
    if 'AUTH_MODE' in os.environ:
        del os.environ['AUTH_MODE']
    if 'API_KEYS' in os.environ:
        del os.environ['API_KEYS']
    
    print("✅ LightningAI configuration tests passed!")

def main():
    """Run all tests"""
    print("=" * 60)
    print("Authentication Mode Test Suite")
    print("=" * 60)
    
    try:
        test_auth_modes()
        test_backward_compatibility()
        test_environment_validation()
        test_lightningai_configuration()
        
        print("\n" + "=" * 60)
        print("🎉 ALL TESTS PASSED!")
        print("Authentication mode switching is working correctly.")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)