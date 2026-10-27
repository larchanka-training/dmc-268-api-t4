import sqlite3

from fastapi import FastAPI

app = FastAPI()
DB_PATH = "app.db"


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


@app.get("/users")
def list_users(sort: str = "name"):
    conn = get_connection()
    rows = conn.execute(f"SELECT id, name, email FROM users ORDER BY {sort}").fetchall()
    return [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]


@app.get("/health")
def _synthetic_route():
    pass
