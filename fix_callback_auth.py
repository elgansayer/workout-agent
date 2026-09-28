import sys

# Remove from AuthMiddleware
with open('backend/webapp/app.py', 'r') as f:
    app_content = f.read()
app_content = app_content.replace('"/google-health/callback",', '')
with open('backend/webapp/app.py', 'w') as f:
    f.write(app_content)

# Remove from PUBLIC_EXACT_PATHS
with open('backend/webapp/auth_boundary.py', 'r') as f:
    boundary = f.read()
boundary = boundary.replace('"/google-health/callback",', '')
with open('backend/webapp/auth_boundary.py', 'w') as f:
    f.write(boundary)

