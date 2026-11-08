#!/usr/bin/env python3
"""
db_versioning.py - Schema Versioning, Compatibility Checking, and Migration Management.

Tracks schema versions using SQLite's native PRAGMA user_version and a detailed
schema_migrations audit log table.
"""

import sys
import sqlite3
import datetime
from pathlib import Path
from typing import Tuple, List, Dict, Optional

# Supported Schema Versions
VERSION_LEGACY = 1    # Basic photos, tags, photo_tags
VERSION_AUTH_AUDIT = 2 # Users, settings, audit_log, tag_permissions, marked_for_removal
VERSION_PROVENANCE = 3 # Origin columns, merge_manifest, merge_conflict_snapshots, tag_origin

CURRENT_SCHEMA_VERSION = VERSION_PROVENANCE


def get_db_connection(db_path_or_conn, mode: str = "rw") -> sqlite3.Connection:
    """Get a connection to SQLite database, respecting read-only or read-write mode."""
    if isinstance(db_path_or_conn, sqlite3.Connection):
        return db_path_or_conn
    p = Path(db_path_or_conn).resolve()
    if mode == "ro":
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15.0)
    return sqlite3.connect(str(p), timeout=15.0)


def detect_schema_version(conn_or_path) -> int:
    """
    Returns the schema version of the database.
    Checks PRAGMA user_version first. If 0, inspects table structure to deduce version.
    """
    close_when_done = False
    if not isinstance(conn_or_path, sqlite3.Connection):
        conn = get_db_connection(conn_or_path, mode="ro")
        close_when_done = True
    else:
        conn = conn_or_path

    try:
        cur = conn.cursor()
        cur.execute("PRAGMA user_version;")
        row = cur.fetchone()
        user_version = row[0] if row else 0

        if user_version > 0:
            return user_version

        # Infer version based on table and column introspection
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r[0] for r in cur.fetchall()}

        if not tables or "photos" not in tables:
            return 0  # Empty or uninitialized database

        # Check for v3 (provenance)
        if "merge_manifest" in tables:
            return VERSION_PROVENANCE
        cur.execute("PRAGMA table_info(photos);")
        photo_cols = {r[1] for r in cur.fetchall()}
        if "origin_sources" in photo_cols:
            return VERSION_PROVENANCE

        # Check for v2 (auth & audit)
        if "users" in tables and "marked_for_removal" in photo_cols:
            return VERSION_AUTH_AUDIT

        # Otherwise v1 (legacy)
        return VERSION_LEGACY
    finally:
        if close_when_done:
            conn.close()


def ensure_migration_table(conn: sqlite3.Connection):
    """Ensure the schema_migrations audit table exists."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL,
            min_app_version TEXT NOT NULL
        );"""
    )


def record_migration(conn: sqlite3.Connection, version: int, description: str, min_app: str = "2.0"):
    """Record a migration event in schema_migrations and update PRAGMA user_version."""
    ensure_migration_table(conn)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT OR REPLACE INTO schema_migrations (version, applied_at, description, min_app_version)
           VALUES (?, ?, ?, ?);""",
        (version, now, description, min_app)
    )
    conn.execute(f"PRAGMA user_version = {version};")


def apply_migration_v2(conn: sqlite3.Connection):
    """
    Applies Schema v2:
    - photos.marked_for_removal
    - settings
    - users
    - user_settings
    - audit_log (with indexes)
    - tag_permissions
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(photos);")
    photo_cols = {r[1] for r in cur.fetchall()}
    if "marked_for_removal" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN marked_for_removal INTEGER NOT NULL DEFAULT 0;")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            access_level INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        );"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
            photo_filename TEXT,
            details TEXT
        );"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_photo ON audit_log(photo_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tag_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_name TEXT UNIQUE NOT NULL,
            min_access_level INTEGER NOT NULL DEFAULT 1,
            restricted_to_role TEXT
        );"""
    )
    record_migration(conn, VERSION_AUTH_AUDIT, "Authentication, audit logging, permissions, and marked_for_removal")


def apply_migration_v3(conn: sqlite3.Connection):
    """
    Applies Schema v3:
    - photos.origin_sources ('A', 'B', 'BOTH', 'MERGED', 'NEW')
    - photos.origin_id_a (INTEGER)
    - photos.origin_id_b (INTEGER)
    - photo_tags.tag_origin ('A', 'B', 'BOTH', 'POST_MERGE')
    - merge_manifest
    - merge_conflict_snapshots
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(photos);")
    photo_cols = {r[1] for r in cur.fetchall()}

    if "origin_sources" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN origin_sources TEXT NOT NULL DEFAULT 'ORIGINAL';")
    if "origin_id_a" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN origin_id_a INTEGER;")
    if "origin_id_b" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN origin_id_b INTEGER;")

    # Check photo_tags table
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='photo_tags';")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(photo_tags);")
        tag_cols = {r[1] for r in cur.fetchall()}
        if "tag_origin" not in tag_cols:
            conn.execute("ALTER TABLE photo_tags ADD COLUMN tag_origin TEXT NOT NULL DEFAULT 'ORIGINAL';")

    # Dedicated Merge Manifest Table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS merge_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merge_timestamp TEXT NOT NULL,
            db_a_name TEXT NOT NULL,
            db_a_sha256 TEXT NOT NULL,
            db_b_name TEXT NOT NULL,
            db_b_sha256 TEXT NOT NULL,
            photos_a_count INTEGER NOT NULL,
            photos_b_count INTEGER NOT NULL,
            overlap_count INTEGER NOT NULL,
            conflict_policy TEXT NOT NULL
        );"""
    )

    # Dedicated Pre-Merge Conflict Snapshots Table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS merge_conflict_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manifest_id INTEGER REFERENCES merge_manifest(id),
            filepath TEXT NOT NULL,
            field_name TEXT NOT NULL,
            val_in_db_a TEXT,
            val_in_db_b TEXT,
            resolved_val TEXT
        );"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conflict_filepath ON merge_conflict_snapshots(filepath);")

    record_migration(conn, VERSION_PROVENANCE, "Lineage tracking, bi-directional merge manifest, and conflict snapshots")


def migrate_database(db_path: str | Path, target_version: int = CURRENT_SCHEMA_VERSION, auto_backup: bool = True) -> int:
    """
    Migrates the database up to target_version.
    If auto_backup is True and migrations are needed, creates a safe backup first.
    Returns the new schema version.
    """
    p = Path(db_path).resolve()
    current_version = detect_schema_version(p)

    if current_version >= target_version:
        # Check if user_version is stamped
        conn = sqlite3.connect(str(p), timeout=15.0)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA user_version;")
            uv = cur.fetchone()[0]
            if uv < current_version:
                conn.execute(f"PRAGMA user_version = {current_version};")
                ensure_migration_table(conn)
                record_migration(conn, current_version, f"Stamped existing schema v{current_version}")
                conn.commit()
        finally:
            conn.close()
        return current_version

    # Migrations are required: take automatic backup first
    if auto_backup:
        from backup_utils import backup_database
        backup_database(p, prefix=f"{p.stem}_pre_migration_v{target_version}")

    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        with conn:
            if current_version < VERSION_AUTH_AUDIT and target_version >= VERSION_AUTH_AUDIT:
                apply_migration_v2(conn)
            if current_version < VERSION_PROVENANCE and target_version >= VERSION_PROVENANCE:
                apply_migration_v3(conn)
    finally:
        conn.close()

    return target_version


def check_compatibility(db_path: str | Path, min_supported: int = VERSION_LEGACY, max_supported: int = CURRENT_SCHEMA_VERSION) -> Tuple[bool, int, str]:
    """
    Checks if a database is compatible with the application.
    Returns (is_compatible, version, message).
    """
    p = Path(db_path)
    if not p.exists():
        return False, 0, f"Database file not found: {p}"

    version = detect_schema_version(p)
    if version < min_supported:
        return False, version, f"Database schema v{version} is below minimum supported v{min_supported}."
    if version > max_supported:
        return False, version, f"Database schema v{version} is newer than max supported v{max_supported}. Please upgrade application."
    return True, version, f"Database schema v{version} is compatible."


def get_migration_history(db_path: str | Path) -> List[Dict]:
    """Return all recorded migrations in the database."""
    conn = get_db_connection(db_path, mode="ro")
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';")
        if not cur.fetchone():
            return []
        cur.execute("SELECT version, applied_at, description, min_app_version FROM schema_migrations ORDER BY version ASC;")
        return [
            {
                "version": r[0],
                "applied_at": r[1],
                "description": r[2],
                "min_app_version": r[3],
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Database Schema Versioning & Migration Manager")
    parser.add_argument("--db", type=str, required=True, help="Path to database")
    parser.add_argument("--check", action="store_true", help="Check schema version & compatibility")
    parser.add_argument("--migrate", action="store_true", help="Migrate database to latest schema version")
    parser.add_argument("--target-version", type=int, default=CURRENT_SCHEMA_VERSION, help=f"Target version (default: {CURRENT_SCHEMA_VERSION})")
    parser.add_argument("--no-backup", action="store_true", help="Disable automatic pre-migration backup")
    parser.add_argument("--history", action="store_true", help="Show migration history")

    args = parser.parse_args()

    if args.history:
        history = get_migration_history(args.db)
        if not history:
            print("No migration history recorded in this database.")
        else:
            print(f"{'Version':<8} {'Applied At':<20} {'App Ver':<10} {'Description'}")
            print("-" * 70)
            for h in history:
                print(f"v{h['version']:<7} {h['applied_at']:<20} {h['min_app_version']:<10} {h['description']}")
        sys.exit(0)

    if args.check or not args.migrate:
        compat, ver, msg = check_compatibility(args.db)
        print(f"Database: {args.db}")
        print(f"Detected Schema Version: v{ver}")
        print(f"Compatibility Status: {'Compatible' if compat else 'Incompatible'} - {msg}")
        if not args.migrate:
            sys.exit(0 if compat else 1)

    if args.migrate:
        print(f"Migrating {args.db} to v{args.target_version}...")
        new_ver = migrate_database(args.db, target_version=args.target_version, auto_backup=not args.no_backup)
        print(f"Successfully updated {args.db} to schema v{new_ver}!")
