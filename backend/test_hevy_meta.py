import sqlite3
from database import init_db

# remove db if exists
import os
if os.path.exists("test_meta.db"):
    os.remove("test_meta.db")

init_db("test_meta.db")
print("FIRST INIT SUCCESS")
init_db("test_meta.db")
print("SECOND INIT SUCCESS")
