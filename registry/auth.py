"""Bearer token creation, hashing, and validation."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("TOKEN_DB_PATH", "/data/db/tokens.db")


def _get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def create_token(name: str) -> str:
    raw = secrets.token_hex(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO tokens (name, token_hash) VALUES (?, ?)",
            (name, hashed),
        )
        conn.commit()
    finally:
        conn.close()
    return raw


def verify_token(raw: str) -> str | None:
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT name FROM tokens WHERE token_hash = ?",
            (hashed,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def require_auth(authorization: str = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    token = authorization[7:]
    name = verify_token(token)
    if not name:
        raise ValueError("Invalid token")
    return name


def main() -> int:
    parser = argparse.ArgumentParser(description="Forge registry auth tools")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create-token", help="Create a new bearer token")
    create.add_argument("--name", default="admin", help="Token owner name")

    args = parser.parse_args()

    if args.command == "create-token":
        token = create_token(args.name)
        print(f"FORGE_TOKEN={token}")
        print(f"Token identity: {args.name}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
