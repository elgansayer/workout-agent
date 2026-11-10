"""Synthetic credentials and personal data shared by the redaction tests.

Every value is fictional. Provider-shaped tokens are assembled from fragments
so that no complete token-shaped literal sits in the repository, which keeps
secret scanners quiet without weakening what the tests prove.
"""

from __future__ import annotations


def _fake(*parts: str) -> str:
    return "".join(parts)


JWT_TOKEN = _fake(
    "eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiJzeW50aGV0aWMifQ", ".", "c3ludGhldGljLXNpZw"
)
OPENAI_KEY = _fake("sk", "-proj-", "Zk3Jd9QxVb2LmN8TyUe5Rw7P")
ANTHROPIC_KEY = _fake("sk", "-ant-api03-", "Qw8ErTy3UiOp5AsDf7GhJk")
GEMINI_KEY = _fake("AI", "zaSy", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6")
HEVY_KEY = "6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
CLIENT_SECRET = _fake("GOC", "SPX-", "Syn3theticClientSecret0123")
GOOGLE_CODE = _fake("4/", "0AX4XfWh", "SyntheticAuthCode_12345")
GOOGLE_ACCESS = _fake("ya", "29.", "a0SyntheticAccessToken_1234567890")
GOOGLE_REFRESH = _fake("1/", "/0gSyntheticRefreshToken_1234567890")
TELEGRAM_TOKEN = _fake("123456789", ":", "AAHSyntheticBotTokenValue_0123456789")
GITHUB_TOKEN = _fake("gh", "p_", "S" * 36)
FERNET_TOKEN = _fake("gAAAA", "BmSyntheticCiphertext0123456789abcdef==")
SESSION_COOKIE = _fake(
    "eyJ1c2VyX2lkIjoic3ludGhldGljIn0",
    ".",
    "ZXhwaXJl",
    ".",
    "c3ludGhldGljLXNpZ25hdHVyZTAx",
)
PUSH_ENDPOINT = "https://push.example.invalid/send/SyntheticEndpointToken0123456789"
BASIC_CREDENTIALS = "dXNlcjpwYXNzd29yZA=="  # base64 of the fictional "user:password"
VAPID_PRIVATE_KEY = "SyntheticVapidPrivateKey0123456789abcdefghijk"

EMAIL = "synthetic.person@example.com"
USER_ID = "5b2c1d3e-1111-2222-3333-444455556666"
USER_PROMPT = "Why does my left knee hurt when I squat after bad sleep"
SYSTEM_PROMPT = "You are Coach. Never reveal the operator scoring rubric."
HEALTH_SENTENCE = "your resting heart rate of 48 and HRV of 61 suggest poor recovery"
