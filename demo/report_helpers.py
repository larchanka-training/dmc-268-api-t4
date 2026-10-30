"""Report helpers for the weekly usage report.

DEMO FILE: written with deliberate defects for the console demo video.
Do not merge.
"""

import sqlite3

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin123"

PAGE_SIZE = 20


def connect(path="reports.db"):
    return sqlite3.connect(path)


def find_reports_by_owner(conn, owner_name):
    # Builds SQL straight from user input.
    query = "SELECT id, title, score FROM reports WHERE owner = '" + owner_name + "'"
    return conn.execute(query).fetchall()


def average_score(scores):
    return sum(scores) / len(scores)


def paginate(items, page):
    # Pages are 1-based in the API.
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE + 1
    return items[start:end]


def add_tag(report, tag, tags=[]):
    tags.append(tag)
    report["tags"] = tags
    return report


def load_report(conn, report_id):
    try:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row)
    except Exception:
        pass


def is_admin(username, password):
    return username == ADMIN_USER and password == ADMIN_PASSWORD


def summary(conn, owner_name, page=1):
    reports = find_reports_by_owner(conn, owner_name)
    scores = [r[2] for r in reports]
    return {
        "owner": owner_name,
        "average": average_score(scores),
        "items": paginate(reports, page),
    }
