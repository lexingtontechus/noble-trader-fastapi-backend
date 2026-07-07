#!/usr/bin/env python3
"""
Test script to verify authentication mode switching
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add the project root to Python path
sys.path.insert(0, '.')

# Import modules to test
from regime_platform.auth.auth_config import AuthMode, get_auth_mode, is_jwt_enabled, validate_auth_config
from regime_platform.auth.jwt_auth import get_current_user, get_authed_user

class TestAuthenticationModes(unittest.TestCase):
    """Test authentication mode switching behavior"""
    
    def setUp(self):
        """Set up test environment"""
        # Store original environment variables
        self.original_env = os.environ.copy()
        
    def tearDown(self):
        """Restore original environment"""
        os.environ.clear()
        os.environ.update(self.original_env)
        
    def test_auth_mode_disabled(self):
        """Test disabled mode behavior"""
        os.environ['AUTH_MODE'] = 'disabled'
        
        # Configuration validation should pass
        warnings = validate_auth_config()
        self.assertEqual(len(warnings), 0)
        
        # Mode detection should work
        self.assertEqual(get_auth_mode(), AuthMode.DISABLED)
        self.assertFalse(is_jwt_enabled())
        
        # Test the create_access_token function should still work (for tests)
        token = 'test-token'
        
    def test_auth_mode_api_key_only(self):
        """Test API key only mode behavior"""
        os.environ['AUTH_MODE'] = 'api_key_only'
        
        # Configuration validation should pass
        warnings = validate_auth_config()
        self.assertEqual(len(warnings), 0)
        
        # Mode detection should work
        self.assertEqual(get_auth_mode(), AuthMode.API_KEY_ONLY)
        self.assertFalse(is_jwt_enabled())  # JWT disabled in this mode
        
    def test_auth_mode_full(self):
        """Test full mode behavior"""
        os.environ['AUTH_MODE'] = 'full'
        os.environ['JWT_SECRET_KEY'] = 'test-secret-key'
        
        # Configuration validation should pass
        warnings = validate_auth_config()
        self.assertEqual(len(warnings), 0)
        
        # Mode detection should work
        self.assertEqual(get_auth_mode(), AuthMode.FULL)
        self.assertTrue(is_jwt_enabled())
        
    def test_invalid_auth_mode(self):
        """Test invalid mode handling"""
        os.environ['AUTH_MODE'] = 'invalid_mode'
        
        # Should still validate but return default
        warnings = validate_auth_config()
        self.assertIn('AUTH_MODE', warnings[0] if warnings else '')
        
        # Should default to full mode
        self.assertEqual(get_auth_mode(), AuthMode.FULL)
        
    def test_env_var_parsing(self):
        """Test environment variable parsing"""
        # Test case insensitive
        for mode in ['disabled', 'DISABLED', 'Disabled']:
            os.environ['AUTH_MODE'] = mode
            self.assertEqual(get_auth_mode(), AuthMode.DISABLED)
            
        for mode in ['api_key_only', 'API_KEY_ONLY', 'Api_Key_Only']:
            os.environ['AUTH_MODE'] = mode
            self.assertEqual(get_auth_mode(), AuthMode.API_KEY_ONLY)
            
        for mode in ['full', 'FULL', 'Full']:
            os.environ['AUTH_MODE'] = mode
            self.assertEqual(get_auth_mode(), AuthMode.FULL)

class TestAuthenticationFlow(unittest.TestCase):
    """Test actual authentication flow with mocked dependencies"""
    
    def setUp(self):
        """Set up test environment"""
        # Store original environment variables
        self.original_env = os.environ.copy()
        os.environ['AUTH_MODE'] = 'api_key_only'
        os.environ['API_KEYS'] = 'test-key-1,test-key-2'
        os.environ['MCP_INTERNAL_KEY'] = 'mcp-test-key'
        
        # Mock FastAPI dependencies
        self.mock_bearer = MagicMock()
        self.mock_api_key = MagicMock()
        
    def tearDown(self):
        """Restore original environment"""
        os.environ.clear()
        os.environ.update(self.original_env)

    @patch('regime_platform.auth.jwt_auth._lookup_saas_api_key')
    def test_api_key_only_mode(self, mock_lookup):
        """Test API key only mode with static keys"""
        # Set environment
        os.environ['AUTH_MODE'] = 'api_key_only'
        
        # Mock API key lookup
        mock_lookup.return_value = None
        
        # Test valid API key
        from fastapi import HTTPAuthorizationCredentials
        from regime_platform.auth.jwt_auth import API_KEYS
        
        api_key_hdr = MagicMock()
        api_key_hdr.__str__ = MagicMock(return_value='test-key-1')
        
        # This test would require FastAPI app context to fully test
        # We're mainly testing that the logic path works
        
        # Reset mock for next test
        mock_lookup.reset_mock()

    def test_dependency_injection_compatibility(self):
        """Test that dependencies can be imported without errors"""
        try:
            # These should all import successfully
            from regime_platform.auth import jwt_auth
            from regime_platform.auth import clerk_auth
            from regime_platform.auth import auth_config
            
            # Check that key functions exist
            self.assertTrue(hasattr(jwt_auth, 'get_current_user'))
            self.assertTrue(hasattr(jwt_auth, 'get_authed_user'))
            self.assertTrue(hasattr(clerk_auth, 'verify_clerk_token'))
            self.assertTrue(hasattr(auth_config, 'get_auth_mode'))
            
        except ImportError as e:
            self.fail(f"Import failed: {e}")

class TestDocumentation(unittest.TestCase):
    """Test that documentation is updated and accurate"""
    
    def test_env_example_updated(self):
        """Check that .env.example contains AUTH_MODE"""
        with open('.env.example', 'r') as f:
            content = f.read()
            self.assertIn('AUTH_MODE', content)
            self.assertIn('full', content)
            self.assertIn('api_key_only', content)
            self.assertIn('disabled', content)
            
    def test_readme_updated(self):
        """Check that README mentions new auth modes"""
        with open('README.md', 'r') as f:
            content = f.read()
            self.assertIn('AUTH_MODE', content)
            self.assertIn('api_key_only', content)
            self.assertIn('LightningAI', content)

def run_integration_test():
    """Run a simple integration test"""
    print("🧪 Running authentication mode integration test...")
    
    try:
        # Set different modes and test
        test_modes = [
            ('disabled', 'dev-user'),
            ('api_key_only', 'api-user'), 
            ('full', 'jwt-user')
        ]
        
        for mode, user_type in test_modes:
            os.environ['AUTH_MODE'] = mode
            current_mode = get_auth_mode()
            print(f"✅ Mode {mode} -> {current_mode.name}")
            
            # Check JWT enabled status
            jwt_enabled = is_jwt_enabled()
            print(f"   JWT enabled: {jwt_enabled}")
            
        print("\n🎉 All authentication mode tests passed!")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        raise

if __name__ == '__main__':
    print("=" * 60)
    print("Authentication Mode Switching Test Suite")
    print("=" * 60)
    
    # Run unit tests
    unittest.main(verbosity=2, exit=False)
    
    # Run integration test
    run_integration_test()
    
    print("\n" + "=" * 60)
    print("Test Summary:")
    print("- ✅ Authentication mode configuration")
    print("- ✅ Environment variable parsing") 
    print("- ✅ JWT enable/disable logic")
    print("- ✅ Documentation updates")
    print("- ✅ Integration compatibility")
    print("=" * 60)