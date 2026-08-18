import re
with open("webapp/app.py", "r") as f:
    content = f.read()
routes_to_comment = ["/login", "/progress", "/stats", "/plan", "/programmes", "/settings", "/chat", "/checkins", "/history"]
for route in routes_to_comment:
    content = re.sub(rf'@app\.get\("{route}"\)', rf'# @app.get("{route}")', content)
with open("webapp/app.py", "w") as f:
    f.write(content)
