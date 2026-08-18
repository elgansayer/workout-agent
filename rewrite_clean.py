with open("webapp/app.py", "r") as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if "get_active_programme," in line and "from programme_state" not in line and "get_active_programme" in line:
        continue
    if "get_programme_templates," in line and "from programme_state" not in line and "get_programme_templates" in line:
        continue
    if "set_active_programme," in line and "from programme_state" not in line and "set_active_programme" in line:
        continue
    if "(today - start).days" in line:
        continue
    if "len(volumes)" in line and "=" not in line:
        continue
    if "sum(v[\"volume\"] for v in volumes)" in line:
        continue
    new_lines.append(line)

with open("webapp/app.py", "w") as f:
    f.writelines(new_lines)
