import sys
import os
from unittest.mock import MagicMock

def test_imports():
    _original_modules = dict(sys.modules)
    _original_env = dict(os.environ)

    try:
        # Mock network-dependent/heavy modules
        sys.modules['sentence_transformers'] = MagicMock()
        sys.modules['supabase'] = MagicMock()

        # Mock environment variables
        os.environ['GROQ_API_KEY'] = 'mock'
        os.environ['SUPABASE_URL'] = 'mock'
        os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'mock'

        # Attempt to import main
        import backend.main

        assert backend.main.app is not None
    finally:
        # Restore ONLY the keys this test explicitly touched.
        # Never remove all backend.* modules generically: that destroys
        # class identity for modules imported naturally by other test
        # files during the same session (e.g. backend.trusted_context,
        # backend.memory, backend.engine).
        for _key in ('backend.main', 'sentence_transformers', 'supabase'):
            _original = _original_modules.get(_key)
            if _original is not None:
                sys.modules[_key] = _original
            elif _key in sys.modules:
                del sys.modules[_key]

        os.environ.clear()
        os.environ.update(_original_env)
