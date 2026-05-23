
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = os.environ.get("REGISTRY_DB_PATH", "/data/db/registry.db")

SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def is_valid_semver(version: str) -> bool:
    return bool(SEMVER_PATTERN.match(version))


def _semver_key(version: str) -> tuple:
    """Sort key for semver ordering (highest last)."""
    from registry.resolver import version_key

    return version_key(version)


def _get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            publisher TEXT NOT NULL,
            deps TEXT DEFAULT '[]',
            published_at TEXT DEFAULT (datetime('now')),
            UNIQUE(name, version)
        )
        """
    )
    conn.commit()
    return conn


def insert_artifact(name, version, sha256, size, publisher, deps=None):
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO artifacts (name, version, sha256, size, publisher, deps) VALUES (?,?,?,?,?,?)",
            (name, version, sha256, size, publisher, json.dumps(deps or [])),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"CONFLICT: {name}@{version} already exists") from exc
    finally:
        conn.close()


def get_artifact(name, version):
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT name, version, sha256, size, publisher, deps, published_at "
            "FROM artifacts WHERE name=? AND version=?",
            (name, version),
        ).fetchone()
        if not row:
            return None
        return {
            "name": row[0],
            "version": row[1],
            "sha256": row[2],
            "size": row[3],
            "publisher": row[4],
            "deps": json.loads(row[5]),
            "published_at": row[6],
        }
    finally:
        conn.close()


def list_versions(name):
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT version, sha256, size, published_at FROM artifacts WHERE name=?",
            (name,),
        ).fetchall()
        entries = [
            {"version": r[0], "sha256": r[1], "size": r[2], "published_at": r[3]}
            for r in rows
        ]
        entries.sort(key=lambda e: _semver_key(e["version"]))
        return entries
    finally:
        conn.close()


def get_all_versions(name):
    return [v["version"] for v in list_versions(name)]
