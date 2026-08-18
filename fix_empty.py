with open("webapp/app.py", "r") as f:
    content = f.read()

content = content.replace("from programme_state import (\n)\n", "")
with open("webapp/app.py", "w") as f:
    f.write(content)
