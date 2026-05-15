#!/usr/bin/env python3
"""First-run admin creation.
Usage: echo "$password" | python3 init_admin.py <username>
"""
import os
import sys

import models
from werkzeug.security import generate_password_hash


def main():
    if len(sys.argv) < 2:
        print("Usage: init_admin.py <username>", file=sys.stderr)
        sys.exit(1)

    username = sys.argv[1].strip()
    password = sys.stdin.read().strip()

    if not username:
        print("ERROR: username is empty", file=sys.stderr)
        sys.exit(1)
    if len(password) < 10:
        print("ERROR: password must be at least 10 characters", file=sys.stderr)
        sys.exit(1)

    db_path = os.environ.get("DB_PATH", "/data/dns.db")
    models.init_db(db_path)

    conn = models.get_db(db_path)
    existing = models.get_user_by_username(conn, username)
    if existing:
        print(f"User '{username}' already exists — skipping creation.")
        conn.close()
        sys.exit(0)

    pw_hash = generate_password_hash(password)
    conn.execute(
        "INSERT INTO users (username, password, is_admin) VALUES (?, ?, 1)",
        (username, pw_hash),
    )
    conn.commit()
    conn.close()
    print(f"Admin user '{username}' created successfully.")


if __name__ == "__main__":
    main()
