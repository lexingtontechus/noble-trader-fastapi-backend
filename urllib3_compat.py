"""
urllib3<2.0 compatibility shim for alpaca-trade-api.

alpaca-trade-api depends on urllib3.packages.six.moves, which was removed
in urllib3>=2.0. This shim recreates the missing module path so that
alpaca-trade-api can be imported successfully.
"""

import sys
import importlib

# Only patch if urllib3 is >= 2.0
try:
    import urllib3
    _urllib3_version = tuple(int(x) for x in urllib3.__version__.split(".")[:2])
    if _urllib3_version >= (2, 0):
        # Create the missing module path
        import urllib3.packages.six
        import types
        # Create urllib3.packages.six.moves if it doesn't exist
        if not hasattr(urllib3.packages.six, "moves"):
            urllib3.packages.six.moves = types.ModuleType("urllib3.packages.six.moves")
        # Also create urllib3.packages.six.moves.urllib
        if not hasattr(urllib3.packages.six.moves, "urllib"):
            urllib3.packages.six.moves.urllib = types.ModuleType("urllib3.packages.six.moves.urllib")
        # Ensure it's accessible via sys.modules
        sys.modules["urllib3.packages.six.moves"] = urllib3.packages.six.moves
        sys.modules["urllib3.packages.six.moves.urllib"] = urllib3.packages.six.moves.urllib
except ImportError:
    pass
except Exception:
    pass
