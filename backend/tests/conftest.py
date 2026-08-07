import os

# Force mock placeholders for tests to prevent real environment keys from being used.
os.environ["APP_ENV"] = "test"
os.environ["GROQ_API_KEY"] = "mock_groq_key_placeholder"
os.environ["GROQ_API_KEY_2"] = "mock_groq_key_2_placeholder"
os.environ["ADMISSION_HMAC_SECRET"] = "test-admission-secret-that-is-at-least-32-bytes"
os.environ["TRUSTED_PROXY_CIDRS"] = ""
