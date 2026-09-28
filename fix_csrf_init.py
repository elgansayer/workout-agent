import sys

with open('backend/webapp/app.py', 'r') as f:
    content = f.read()

patch_from = """app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# Always mount SessionMiddleware so templates and routes can safely access
# request.session even when web auth is disabled. Without it, any access to
# request.session raises an AssertionError -> 500 Internal Server Error.
# When WEB_AUTH_SECRET is unset we fall back to a per-process random key; this
# is fine because there is nothing sensitive to protect when auth is off.
app.add_middleware(
    SessionMiddleware,
    secret_key=WEB_AUTH_SECRET or secrets.token_hex(32),
)"""

patch_to = """_session_secret = WEB_AUTH_SECRET or secrets.token_hex(32)

app.add_middleware(
    CSRFMiddleware,
    db_path=DB_PATH,
    session_secret=_session_secret,
)
app.add_middleware(SecurityHeadersMiddleware)

# Always mount SessionMiddleware so templates and routes can safely access
# request.session even when web auth is disabled. Without it, any access to
# request.session raises an AssertionError -> 500 Internal Server Error.
# When WEB_AUTH_SECRET is unset we fall back to a per-process random key; this
# is fine because there is nothing sensitive to protect when auth is off.
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
)"""

if patch_from in content:
    content = content.replace(patch_from, patch_to)
else:
    print("Failed to find patch_from in app.py")

with open('backend/webapp/app.py', 'w') as f:
    f.write(content)
