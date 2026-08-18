import re

with open("webapp/app.py", "r") as f:
    content = f.read()

# 1. Strip Jinja setup
content = re.sub(r'from fastapi\.templating import Jinja2Templates\n', '', content)
content = re.sub(r'templates = Jinja2Templates\(directory=str\(_BASE_DIR / "templates"\)\)\n', '', content)
content = re.sub(r'templates\.env\.globals\["static_url"\] = static_url\n', '', content)

# 2. Add FRONTEND_DIST
content = re.sub(r'_BASE_DIR = Path\(__file__\)\.resolve\(\)\.parent\n',
                 '_BASE_DIR = Path(__file__).resolve().parent\nFRONTEND_DIST = _BASE_DIR.parent / "frontend/dist/frontend/browser"\n',
                 content)

# 3. Delete routes that return TemplateResponse
# We will match the route and the entire function body until the next `@app.get` or `@app.post` or end of file.
# Since python functions might have arbitrary inner structure, we can just split the file by `@app.` and filter.

blocks = re.split(r'(?=@app\.(?:get|post)\()', content)
new_blocks = [blocks[0]] # the stuff before the first route

for block in blocks[1:]:
    if 'return templates.TemplateResponse' in block:
        # We skip this block completely!
        continue
    new_blocks.append(block)

content = "".join(new_blocks)

# 4. We still need to keep `/login` if it had some logic, but wait! `/login` just returns the Jinja template!
# Wait! `login` route:
# @app.get("/login")
# async def login(request: Request) -> Any:
#     if not WEB_GOOGLE_CLIENT_ID:
#         return HTMLResponse("Web auth is not configured.", status_code=500)
#     if request.session.get("user"):
#         return RedirectResponse("/")
#     return templates.TemplateResponse(request, "login.html")
# We deleted it. That's FINE, because `/login` will be caught by `catch_all` and Angular will render its login page, which has a button that hits `/login/google` which is STILL present.

# 5. Add catch-all and angular mount at the end
angular_mount = """
@app.get("/{full_path:path}")
async def catch_all(full_path: str, request: Request):
    if full_path.startswith(("api/", "static/", "login/google", "auth", "google-health/")):
        raise HTTPException(status_code=404, detail="Not found")
    
    if full_path:
        file_path = FRONTEND_DIST / full_path
        if file_path.is_file():
            return FileResponse(file_path)
            
    return FileResponse(FRONTEND_DIST / "index.html")
"""

content += angular_mount

with open("webapp/app.py", "w") as f:
    f.write(content)
