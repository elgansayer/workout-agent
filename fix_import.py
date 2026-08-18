with open('webapp/app.py', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "from database import (" in line:
        lines.insert(i+1, "    get_active_programme,\n    get_programme_templates,\n    set_active_programme,\n")
        break

with open('webapp/app.py', 'w') as f:
    f.writelines(lines)
