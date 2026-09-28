import sys

with open('backend/webapp/app.py', 'r') as f:
    content = f.read()

content = content.replace("get_dashboard_insight(db_path=DB_PATH)", "get_dashboard_insight(db_path=DB_PATH, user_id=user_id)")
content = content.replace("get_personal_records(db_path=DB_PATH)", "get_personal_records(db_path=DB_PATH, user_id=user_id)")

with open('backend/webapp/app.py', 'w') as f:
    f.write(content)
