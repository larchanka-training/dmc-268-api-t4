import sqlite3

DB_PATH = "app.db"
SORT_COLUMNS = {"name": "name", "created": "created_at", "email": "email"}


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def list_users(sort: str = "name", domain: str | None = None) -> list[tuple]:
    column = SORT_COLUMNS.get(sort, "name")
    query = "SELECT id, name, email FROM users"
    params: list[str] = []
    if domain:
        query += " WHERE email LIKE ?"
        params.append(f"%@{domain}")
    query += f" ORDER BY {column}"
    return get_connection().execute(query, params).fetchall()
