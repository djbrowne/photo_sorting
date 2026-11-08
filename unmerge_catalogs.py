#!/usr/bin/env python3
"""
unmerge_catalogs.py - Reverses a merged photo catalog database cleanly.

Features:
- Source merged database is opened strictly in read-only mode.
- Restores original photo integer IDs from origin_id_a and origin_id_b.
- Two operational strategies:
  * rollback: Restores exact pre-merge state using merge_conflict_snapshots.
  * forward: Distributes post-merge tags and edits to the respective target catalogs.
"""

import sys
import time
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

from db_versioning import (
    VERSION_LEGACY,
    VERSION_AUTH_AUDIT,
    get_db_connection,
    record_migration,
)


def unmerge_database(
    merged_db_path: Path,
    out_a_path: Optional[Path] = None,
    out_b_path: Optional[Path] = None,
    strategy: str = "rollback",
) -> None:
    """
    Safely un-merges merged_db_path into out_a_path and/or out_b_path.
    """
    start_time = time.time()
    merged_path = Path(merged_db_path).resolve()
    if not merged_path.exists():
        raise FileNotFoundError(f"Merged database not found: {merged_path}")

    if not out_a_path and not out_b_path:
        raise ValueError("Must specify at least one of --out-a or --out-b.")

    conn_merged = get_db_connection(merged_path, mode="ro")
    cur_m = conn_merged.cursor()

    try:
        # Verify provenance columns exist
        cur_m.execute("PRAGMA table_info(photos);")
        cols = {r[1] for r in cur_m.fetchall()}
        if "origin_sources" not in cols or "origin_id_a" not in cols or "origin_id_b" not in cols:
            raise ValueError(f"Database {merged_path} is not a valid merged catalog with provenance columns.")

        # Load conflict snapshots if rollback strategy
        conflict_map_a: Dict[str, Dict[str, str]] = {}
        conflict_map_b: Dict[str, Dict[str, str]] = {}
        cur_m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='merge_conflict_snapshots';")
        if cur_m.fetchone():
            cur_m.execute("SELECT filepath, field_name, val_in_db_a, val_in_db_b FROM merge_conflict_snapshots;")
            for fp, fld, val_a, val_b in cur_m.fetchall():
                if val_a is not None:
                    conflict_map_a.setdefault(fp, {})[fld] = val_a
                if val_b is not None:
                    conflict_map_b.setdefault(fp, {})[fld] = val_b

        # -------------------------------------------------------------
        # Un-merge to Catalog A
        # -------------------------------------------------------------
        if out_a_path:
            out_a = Path(out_a_path).resolve()
            if out_a == merged_path:
                raise ValueError("Cannot overwrite merged database file!")
            if out_a.exists():
                out_a.unlink()

            print(f"Extracting Catalog A ({strategy} mode) -> {out_a}...")
            _extract_catalog(
                conn_merged=conn_merged,
                out_path=out_a,
                target_origin="A",
                strategy=strategy,
                conflict_map=conflict_map_a,
                schema_version=VERSION_AUTH_AUDIT,
            )
            print(f"Catalog A restored successfully: {out_a}")

        # -------------------------------------------------------------
        # Un-merge to Catalog B
        # -------------------------------------------------------------
        if out_b_path:
            out_b = Path(out_b_path).resolve()
            if out_b == merged_path:
                raise ValueError("Cannot overwrite merged database file!")
            if out_b.exists():
                out_b.unlink()

            print(f"Extracting Catalog B ({strategy} mode) -> {out_b}...")
            _extract_catalog(
                conn_merged=conn_merged,
                out_path=out_b,
                target_origin="B",
                strategy=strategy,
                conflict_map=conflict_map_b,
                schema_version=VERSION_LEGACY,
            )
            print(f"Catalog B restored successfully: {out_b}")

        print(f"Un-merge completed in {time.time() - start_time:.2f} seconds.")

    finally:
        conn_merged.close()


def _extract_catalog(
    conn_merged: sqlite3.Connection,
    out_path: Path,
    target_origin: str,  # 'A' or 'B'
    strategy: str,
    conflict_map: Dict[str, Dict[str, str]],
    schema_version: int,
):
    """Internal helper to extract a single catalog with original IDs."""
    cur_m = conn_merged.cursor()
    conn_out = sqlite3.connect(str(out_path), timeout=60.0)
    cur_out = conn_out.cursor()

    try:
        conn_out.execute("PRAGMA synchronous = OFF;")
        conn_out.execute("PRAGMA journal_mode = MEMORY;")

        # Create schema for target
        cur_out.executescript(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                size_bytes INTEGER,
                width INTEGER,
                height INTEGER,
                date_taken TEXT,
                gps_lat REAL,
                gps_lon REAL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                processed_at TEXT,
                marked_for_removal INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE photo_tags (
                photo_id INTEGER NOT NULL REFERENCES photos(id),
                tag_id INTEGER NOT NULL REFERENCES tags(id),
                PRIMARY KEY (photo_id, tag_id)
            );
            """
        )

        if schema_version >= VERSION_AUTH_AUDIT:
            cur_out.executescript(
                """
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    access_level INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_login TEXT
                );
                CREATE TABLE user_settings (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );
                CREATE TABLE audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
                    photo_filename TEXT,
                    details TEXT
                );
                CREATE TABLE tag_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_name TEXT UNIQUE NOT NULL,
                    min_access_level INTEGER NOT NULL DEFAULT 1,
                    restricted_to_role TEXT
                );
                """
            )

        # Select records relevant to target_origin
        orig_id_col = f"origin_id_{target_origin.lower()}"
        cur_m.execute(
            f"""SELECT id, filepath, filename, size_bytes, width, height, date_taken, gps_lat, gps_lon,
                       description, status, error_message, processed_at, marked_for_removal,
                       origin_sources, {orig_id_col}
                FROM photos
                WHERE origin_sources = ? OR origin_sources = 'BOTH'
                {f"OR origin_sources = 'POST_MERGE'" if strategy == 'forward' else ""};""",
            (target_origin,)
        )

        rows = cur_m.fetchall()
        print(f"Extracted {len(rows):,} photos for Catalog {target_origin}.")

        insert_photo_sql = """
            INSERT INTO photos (
                id, filepath, filename, size_bytes, width, height, date_taken, gps_lat, gps_lon,
                description, status, error_message, processed_at, marked_for_removal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """

        merged_id_to_restored_id: Dict[int, int] = {}

        for r in rows:
            (m_id, fp, fn, sz, w, h, dt, lat, lon, desc, st, err, proc, rem, orig_src, orig_id) = r

            # Determine ID: use original ID if present, otherwise let SQLite assign or use m_id
            restored_id = orig_id if orig_id is not None else m_id
            merged_id_to_restored_id[m_id] = restored_id

            # Apply rollback overrides if strategy is rollback and record was conflicting
            if strategy == "rollback" and fp in conflict_map:
                field_overrides = conflict_map[fp]
                if "description" in field_overrides:
                    desc = field_overrides["description"]
                if "status" in field_overrides:
                    st = field_overrides["status"]
                if "marked_for_removal" in field_overrides:
                    rem = int(field_overrides["marked_for_removal"])

            cur_out.execute(
                insert_photo_sql,
                (restored_id, fp, fn, sz, w, h, dt, lat, lon, desc, st, err, proc, rem)
            )

        # -------------------------------------------------------------
        # Restore Tags
        # -------------------------------------------------------------
        # Select tag relations
        tag_filter = f"tag_origin = '{target_origin}' OR tag_origin = 'BOTH'"
        if strategy == "forward":
            tag_filter += " OR tag_origin = 'POST_MERGE'"

        cur_m.execute(
            f"""SELECT pt.photo_id, t.name
                FROM photo_tags pt
                JOIN tags t ON pt.tag_id = t.id
                WHERE {tag_filter};"""
        )

        tag_name_to_id: Dict[str, int] = {}
        for m_pid, tname in cur_m.fetchall():
            if m_pid not in merged_id_to_restored_id:
                continue
            restored_pid = merged_id_to_restored_id[m_pid]

            if tname not in tag_name_to_id:
                cur_out.execute("INSERT OR IGNORE INTO tags (name) VALUES (?);", (tname,))
                if cur_out.lastrowid:
                    tag_name_to_id[tname] = cur_out.lastrowid
                else:
                    cur_out.execute("SELECT id FROM tags WHERE name = ?;", (tname,))
                    tag_name_to_id[tname] = cur_out.fetchone()[0]

            tid = tag_name_to_id[tname]
            cur_out.execute(
                "INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?);",
                (restored_pid, tid)
            )

        # -------------------------------------------------------------
        # Restore Auth/Audit if applicable
        # -------------------------------------------------------------
        if schema_version >= VERSION_AUTH_AUDIT:
            # Settings
            cur_m.execute("SELECT key, value FROM settings;")
            for k, v in cur_m.fetchall():
                cur_out.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);", (k, v))

            # Users
            cur_m.execute("SELECT id, username, password_hash, role, access_level, created_at, last_login FROM users;")
            for u in cur_m.fetchall():
                cur_out.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?);", u)

            # Audit log
            cur_m.execute("SELECT id, timestamp, user_id, username, action, photo_id, photo_filename, details FROM audit_log;")
            for aid, ts, uid, uname, act, pid, pfn, det in cur_m.fetchall():
                restored_pid = merged_id_to_restored_id.get(pid) if pid else None
                cur_out.execute(
                    "INSERT INTO audit_log (timestamp, user_id, username, action, photo_id, photo_filename, details) VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (ts, uid, uname, act, restored_pid, pfn, det)
                )

        # Create standard indices
        cur_out.execute("CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken DESC, id DESC);")
        cur_out.execute("CREATE INDEX IF NOT EXISTS idx_photos_filepath ON photos(filepath);")
        if schema_version >= VERSION_AUTH_AUDIT:
            cur_out.execute("CREATE INDEX IF NOT EXISTS idx_audit_photo ON audit_log(photo_id);")
            cur_out.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);")

        record_migration(conn_out, schema_version, f"Restored via unmerge utility ({strategy} mode)")
        conn_out.commit()

        # Reset journal mode
        conn_out.execute("PRAGMA journal_mode = WAL;")
        conn_out.execute("PRAGMA synchronous = NORMAL;")

    finally:
        conn_out.close()


def main():
    parser = argparse.ArgumentParser(description="Unmerge photo catalog databases")
    parser.add_argument("merged_db", type=Path, help="Path to merged database file")
    parser.add_argument("--out-a", type=Path, default=None, help="Output path for restored Catalog A")
    parser.add_argument("--out-b", type=Path, default=None, help="Output path for restored Catalog B")
    parser.add_argument("--strategy", choices=["rollback", "forward"], default="rollback", help="Un-merge strategy: 'rollback' (exact pre-merge values) or 'forward' (preserve post-merge edits)")

    args = parser.parse_args()
    unmerge_database(args.merged_db, out_a_path=args.out_a, out_b_path=args.out_b, strategy=args.strategy)


if __name__ == "__main__":
    main()
