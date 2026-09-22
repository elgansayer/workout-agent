import sys

with open('backend/webapp/app.py', 'r') as f:
    content = f.read()

imports_patch = """
from webapp import ai_widgets, charts
from webapp.security_headers import SecurityHeadersMiddleware
from webapp.csrf_security import CSRFMiddleware
"""
content = content.replace("from webapp import ai_widgets, charts", imports_patch.strip())

middleware_patch = """
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# Always mount SessionMiddleware so templates and routes can safely access
"""
content = content.replace("# Always mount SessionMiddleware so templates and routes can safely access", middleware_patch.strip())

with open('backend/webapp/app.py', 'w') as f:
    f.write(content)
