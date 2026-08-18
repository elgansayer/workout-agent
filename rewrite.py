import re

with open("webapp/app.py", "r") as f:
    content = f.read()

# 1. Strip Jinja imports
content = re.sub(r'from fastapi\.templating import Jinja2Templates\n', '', content)
content = re.sub(r'templates = Jinja2Templates\(directory=str\(_BASE_DIR / "templates"\)\)\n', '', content)
content = re.sub(r'templates\.env\.globals\["static_url"\] = static_url\n', '', content)

# 2. Add FRONTEND_DIST
content = re.sub(r'_BASE_DIR = Path\(__file__\)\.resolve\(\)\.parent\n',
                 '_BASE_DIR = Path(__file__).resolve().parent\nFRONTEND_DIST = _BASE_DIR.parent / "frontend/dist/frontend/browser"\n',
                 content)

# 3. Replace all return templates.TemplateResponse(...) with return FileResponse(...)
# We match `return templates.TemplateResponse(` and then match everything until the balancing `)` 
# but actually we can just match `return templates.TemplateResponse\([^)]+\)` if we are careful, 
# but wait! some function calls inside might have `()`.
# Let's write a simple python loop to find `return templates.TemplateResponse(` and balance parenthesis.

idx = content.find('return templates.TemplateResponse(')
while idx != -1:
    paren_count = 0
    start = idx + len('return templates.TemplateResponse')
    i = start
    for i in range(start, len(content)):
        if content[i] == '(':
            paren_count += 1
        elif content[i] == ')':
            paren_count -= 1
            if paren_count == 0:
                break
    end = i + 1
    content = content[:idx] + 'return FileResponse(FRONTEND_DIST / "index.html")' + content[end:]
    idx = content.find('return templates.TemplateResponse(')

# 4. Add catch-all and angular mount at the end
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
