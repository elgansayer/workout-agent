import re

app_path = "webapp/app.py"
with open(app_path, "r") as f:
    content = f.read()

# Remove Jinja imports
content = re.sub(r"from fastapi\.templating import Jinja2Templates\n", "", content)

# Remove templates initialization
content = re.sub(r'_BASE_DIR = Path\(__file__\)\.resolve\(\)\.parent\n', '_BASE_DIR = Path(__file__).resolve().parent\nFRONTEND_DIST = _BASE_DIR.parent / "frontend/dist/frontend/browser"\n', content)
content = re.sub(r'templates = Jinja2Templates\(directory=str\(_BASE_DIR / "templates"\)\)\n', '', content)
content = re.sub(r'templates\.env\.globals\["static_url"\] = static_url\n', '', content)

# Rewrite all endpoints that return TemplateResponse to return FileResponse for index.html
# Example: @app.get("/")\n... return templates.TemplateResponse(...)
# We will just replace `return templates.TemplateResponse(...)` with `return FileResponse(FRONTEND_DIST / "index.html")`

content = re.sub(r'return templates\.TemplateResponse\([^)]+\)', 'return FileResponse(FRONTEND_DIST / "index.html")', content)

with open(app_path, "w") as f:
    f.write(content)

