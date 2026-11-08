#!/usr/bin/env python3
"""
merge_catalogs.py - Safe, non-destructive photo catalog database merger.

Features:
- Source databases are strictly opened read-only (zero risk of corruption).
- Full provenance tracking (origin_sources, origin_id_a, origin_id_b, tag_origin).
- Pre-merge conflict snapshots for reversible un-merging.
- SHA-256 manifest logging.
- Set-union deduplication of photos and tags.
- Auto-backup integration.
"""

import os
import sys
import time
import hashlib
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Set, Tuple, Any, Optional

from db_versioning import (
    CURRENT_SCHEMA_VERSION,
    VERSION_PROVENANCE,
    get_db_connection,
    detect_schema_version,
    record_migration,
)
from backup_utils import backup_database


def compute_sha256(filepath: Path) -> str:
    """Compute SHA-256 hash of a database file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def analyze_catalogs(db_a_path: Path, db_b_path: Path) -> Dict[str, Any]:
    """
    Analyzes two catalogs in read-only mode and computes overlap and conflict statistics.
    """
    conn_a = get_db_connection(db_a_path, mode="ro")
    conn_b = get_db_connection(db_b_path, mode="ro")

    try:
        cur_a = conn_a.cursor()
        cur_b = conn_b.cursor()

        # Count total photos
        cur_a.execute("SELECT count(*), count(distinct filepath) FROM photos;")
        count_a, distinct_a = cur_a.fetchone()

        cur_b.execute("SELECT count(*), count(distinct filepath) FROM photos;")
        count_b, distinct_b = cur_b.fetchone()

        # Map filepaths to IDs and metadata
        cur_a.execute("PRAGMA table_info(photos);")
        cols_a_names = {r[1] for r in cur_a.fetchall()}
        rem_a_sql = "marked_for_removal" if "marked_for_removal" in cols_a_names else "0 AS marked_for_removal"
        cur_a.execute(f"SELECT id, filepath, description, status, {rem_a_sql} FROM photos;")
        photos_a = {r[1]: (r[0], r[2], r[3], r[4]) for r in cur_a.fetchall()}

        cur_b.execute("PRAGMA table_info(photos);")
        cols_b_names = {r[1] for r in cur_b.fetchall()}
        rem_b_sql = "marked_for_removal" if "marked_for_removal" in cols_b_names else "0 AS marked_for_removal"
        cur_b.execute(f"SELECT id, filepath, description, status, {rem_b_sql} FROM photos;")
        photos_b = {r[1]: (r[0], r[2], r[3], r[4]) for r in cur_b.fetchall()}

        paths_a = set(photos_a.keys())
        paths_b = set(photos_b.keys())

        only_a = paths_a - paths_b
        only_b = paths_b - paths_a
        shared = paths_a & paths_b

        # Analyze conflicts in shared photos
        desc_conflicts = 0
        status_conflicts = 0
        removal_conflicts = 0
        for p in shared:
            _, desc_a, st_a, rem_a = photos_a[p]
            _, desc_b, st_b, rem_b = photos_b[p]
            if desc_a != desc_b and desc_a and desc_b:
                desc_conflicts += 1
            if st_a != st_b:
                status_conflicts += 1
            if rem_a != rem_b:
                removal_conflicts += 1

        # Tags analysis
        cur_a.execute("SELECT name FROM tags;")
        tags_a = {r[0] for r in cur_a.fetchall()}

        cur_b.execute("SELECT name FROM tags;")
        tags_b = {r[0] for r in cur_b.fetchall()}

        union_tags = tags_a | tags_b

        return {
            "db_a": str(db_a_path),
            "db_b": str(db_b_path),
            "version_a": detect_schema_version(conn_a),
            "version_b": detect_schema_version(conn_b),
            "total_a": count_a,
            "total_b": count_b,
            "only_a": len(only_a),
            "only_b": len(only_b),
            "shared": len(shared),
            "total_merged_estimate": len(paths_a | paths_b),
            "desc_conflicts": desc_conflicts,
            "status_conflicts": status_conflicts,
            "removal_conflicts": removal_conflicts,
            "tags_a": len(tags_a),
            "tags_b": len(tags_b),
            "union_tags": len(union_tags),
        }
    finally:
        conn_a.close()
        conn_b.close()


def print_analysis_report(stats: Dict[str, Any]):
    """Pretty prints the analysis comparison report."""
    print("=" * 72)
    print("CATALOG COMPARISON & DRY-RUN ANALYSIS")
    print("=" * 72)
    print(f"Catalog A: {stats['db_a']} (Schema v{stats['version_a']})")
    print(f"Catalog B: {stats['db_b']} (Schema v{stats['version_b']})")
    print("-" * 72)
    print(f"Total Photos in Catalog A:  {stats['total_a']:>8,}")
    print(f"Total Photos in Catalog B:  {stats['total_b']:>8,}")
    print(f"Photos Unique to Catalog A: {stats['only_a']:>8,}")
    print(f"Photos Unique to Catalog B: {stats['only_b']:>8,}")
    print(f"Photos in Both Catalogs:    {stats['shared']:>8,}")
    print(f"Estimated Merged Photos:    {stats['total_merged_estimate']:>8,}")
    print("-" * 72)
    print("Conflict Potential in Shared Records:")
    print(f"  - Differing Descriptions: {stats['desc_conflicts']:>8,}")
    print(f"  - Differing Status:       {stats['status_conflicts']:>8,}")
    print(f"  - Differing Removal Flag: {stats['removal_conflicts']:>8,}")
    print("-" * 72)
    print("Tag Metrics:")
    print(f"  - Unique Tags in A:       {stats['tags_a']:>8,}")
    print(f"  - Unique Tags in B:       {stats['tags_b']:>8,}")
    print(f"  - Unified Tag Vocabulary: {stats['union_tags']:>8,}")
    print("=" * 72)


def create_merged_database_schema(conn: sqlite3.Connection):
    """Initializes the empty merged database with Schema v3."""
    cur = conn.cursor()
    cur.executescript(
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
            marked_for_removal INTEGER NOT NULL DEFAULT 0,
            origin_sources TEXT NOT NULL DEFAULT 'MERGED',
            origin_id_a INTEGER,
            origin_id_b INTEGER
        );

        CREATE TABLE tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE photo_tags (
            photo_id INTEGER NOT NULL REFERENCES photos(id),
            tag_id INTEGER NOT NULL REFERENCES tags(id),
            tag_origin TEXT NOT NULL DEFAULT 'MERGED',
            PRIMARY KEY (photo_id, tag_id)
        );

        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

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

        CREATE TABLE merge_manifest (
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
        );

        CREATE TABLE merge_conflict_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manifest_id INTEGER REFERENCES merge_manifest(id),
            filepath TEXT NOT NULL,
            field_name TEXT NOT NULL,
            val_in_db_a TEXT,
            val_in_db_b TEXT,
            resolved_val TEXT
        );

        CREATE INDEX idx_photos_date ON photos(date_taken DESC, id DESC);
        CREATE INDEX idx_photos_filepath ON photos(filepath);
        CREATE INDEX idx_photos_origin ON photos(origin_sources);
        CREATE INDEX idx_audit_photo ON audit_log(photo_id);
        CREATE INDEX idx_audit_time ON audit_log(timestamp DESC);
        CREATE INDEX idx_conflict_filepath ON merge_conflict_snapshots(filepath);
        """
    )
    record_migration(conn, VERSION_PROVENANCE, "Initial merged catalog with full provenance tracking")


def execute_merge(
    db_a_path: Path,
    db_b_path: Path,
    out_db_path: Path,
    conflict_policy: str = "smart_merge",
) -> None:
    """
    Executes non-destructive merge of db_a and db_b into out_db_path.
    """
    start_time = time.time()
    db_a_path = db_a_path.resolve()
    db_b_path = db_b_path.resolve()
    out_db_path = out_db_path.resolve()

    if out_db_path in (db_a_path, db_b_path):
        raise ValueError("Output database cannot be the same file as either input database!")

    if out_db_path.exists():
        raise FileExistsError(f"Target database already exists: {out_db_path}. Delete or choose another path.")

    print(f"Computing source checksums...")
    sha_a = compute_sha256(db_a_path)
    sha_b = compute_sha256(db_b_path)

    conn_a = get_db_connection(db_a_path, mode="ro")
    conn_b = get_db_connection(db_b_path, mode="ro")
    conn_out = sqlite3.connect(str(out_db_path), timeout=60.0)

    try:
        conn_out.execute("PRAGMA synchronous = OFF;")
        conn_out.execute("PRAGMA journal_mode = MEMORY;")

        print("Initializing merged schema (v3)...")
        create_merged_database_schema(conn_out)

        # 1. Record Manifest
        cur_a = conn_a.cursor()
        cur_b = conn_b.cursor()
        cur_out = conn_out.cursor()

        cur_a.execute("SELECT count(*) FROM photos;")
        count_a = cur_a.fetchone()[0]
        cur_b.execute("SELECT count(*) FROM photos;")
        count_b = cur_b.fetchone()[0]

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        cur_out.execute(
            """INSERT INTO merge_manifest
               (merge_timestamp, db_a_name, db_a_sha256, db_b_name, db_b_sha256, photos_a_count, photos_b_count, overlap_count, conflict_policy)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?);""",
            (now_str, db_a_path.name, sha_a, db_b_path.name, sha_b, count_a, count_b, conflict_policy)
        )
        manifest_id = cur_out.lastrowid

        # 2. Load all photos from A and B into memory mappings
        print("Reading records from Catalog A...")
        cur_a.execute(
            """SELECT id, filepath, filename, size_bytes, width, height, date_taken, gps_lat, gps_lon,
                      description, status, error_message, processed_at, marked_for_removal FROM photos;"""
        )
        cols_a = [d[0] for d in cur_a.description]
        records_a = {row[1]: dict(zip(cols_a, row)) for row in cur_a.fetchall()}

        print("Reading records from Catalog B...")
        cur_b.execute("PRAGMA table_info(photos);")
        b_cols_names = {r[1] for r in cur_b.fetchall()}
        has_rem_b = "marked_for_removal" in b_cols_names

        b_query = f"""SELECT id, filepath, filename, size_bytes, width, height, date_taken, gps_lat, gps_lon,
                             description, status, error_message, processed_at
                             {', marked_for_removal' if has_rem_b else ', 0 AS marked_for_removal'}
                      FROM photos;"""
        cur_b.execute(b_query)
        cols_b = [d[0] for d in cur_b.description]
        records_b = {row[1]: dict(zip(cols_b, row)) for row in cur_b.fetchall()}

        all_paths = sorted(set(records_a.keys()) | set(records_b.keys()))
        shared_paths = set(records_a.keys()) & set(records_b.keys())

        # Update overlap count in manifest
        cur_out.execute("UPDATE merge_manifest SET overlap_count = ? WHERE id = ?;", (len(shared_paths), manifest_id))

        print(f"Merging {len(all_paths):,} total photo records ({len(shared_paths):,} shared)...")

        # ID Mapping caches for relational tables (photo_tags, audit_log)
        # map_a[orig_id_a] = new_id, map_b[orig_id_b] = new_id
        map_a: Dict[int, int] = {}
        map_b: Dict[int, int] = {}

        insert_photo_sql = """
            INSERT INTO photos (
                filepath, filename, size_bytes, width, height, date_taken, gps_lat, gps_lon,
                description, status, error_message, processed_at, marked_for_removal,
                origin_sources, origin_id_a, origin_id_b
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """

        conflict_sql = """
            INSERT INTO merge_conflict_snapshots (manifest_id, filepath, field_name, val_in_db_a, val_in_db_b, resolved_val)
            VALUES (?, ?, ?, ?, ?, ?);
        """

        for filepath in all_paths:
            in_a = filepath in records_a
            in_b = filepath in records_b

            if in_a and not in_b:
                rec = records_a[filepath]
                cur_out.execute(
                    insert_photo_sql,
                    (
                        rec["filepath"], rec["filename"], rec["size_bytes"], rec["width"], rec["height"],
                        rec["date_taken"], rec["gps_lat"], rec["gps_lon"], rec["description"], rec["status"],
                        rec["error_message"], rec["processed_at"], rec["marked_for_removal"],
                        "A", rec["id"], None
                    )
                )
                new_id = cur_out.lastrowid
                map_a[rec["id"]] = new_id

            elif in_b and not in_a:
                rec = records_b[filepath]
                cur_out.execute(
                    insert_photo_sql,
                    (
                        rec["filepath"], rec["filename"], rec["size_bytes"], rec["width"], rec["height"],
                        rec["date_taken"], rec["gps_lat"], rec["gps_lon"], rec["description"], rec["status"],
                        rec["error_message"], rec["processed_at"], rec["marked_for_removal"],
                        "B", None, rec["id"]
                    )
                )
                new_id = cur_out.lastrowid
                map_b[rec["id"]] = new_id

            else:
                # In both catalogs! Resolve conflicts.
                rec_a = records_a[filepath]
                rec_b = records_b[filepath]

                # 1. Removal flag: if 1 in either, set 1
                rem_a = rec_a["marked_for_removal"] or 0
                rem_b = rec_b["marked_for_removal"] or 0
                resolved_rem = max(rem_a, rem_b)
                if rem_a != rem_b:
                    cur_out.execute(conflict_sql, (manifest_id, filepath, "marked_for_removal", str(rem_a), str(rem_b), str(resolved_rem)))

                # 2. Status: prefer 'done' over 'error' or 'pending'
                st_a = rec_a["status"] or "pending"
                st_b = rec_b["status"] or "pending"
                resolved_st = "done" if "done" in (st_a, st_b) else (st_a or st_b)
                if st_a != st_b:
                    cur_out.execute(conflict_sql, (manifest_id, filepath, "status", st_a, st_b, resolved_st))

                # 3. Description: choose longer or non-empty
                desc_a = rec_a["description"] or ""
                desc_b = rec_b["description"] or ""
                if desc_a == desc_b:
                    resolved_desc = desc_a or None
                elif not desc_a:
                    resolved_desc = desc_b or None
                elif not desc_b:
                    resolved_desc = desc_a or None
                else:
                    resolved_desc = desc_a if len(desc_a) >= len(desc_b) else desc_b
                    cur_out.execute(conflict_sql, (manifest_id, filepath, "description", desc_a, desc_b, resolved_desc))

                # 4. EXIF backfill
                resolved_date = rec_a["date_taken"] or rec_b["date_taken"]
                resolved_lat = rec_a["gps_lat"] if rec_a["gps_lat"] is not None else rec_b["gps_lat"]
                resolved_lon = rec_a["gps_lon"] if rec_a["gps_lon"] is not None else rec_b["gps_lon"]
                resolved_w = rec_a["width"] or rec_b["width"]
                resolved_h = rec_a["height"] or rec_b["height"]
                resolved_sz = rec_a["size_bytes"] or rec_b["size_bytes"]
                resolved_err = rec_a["error_message"] if resolved_st == "error" else None
                resolved_proc = rec_a["processed_at"] or rec_b["processed_at"]

                cur_out.execute(
                    insert_photo_sql,
                    (
                        filepath, rec_a["filename"], resolved_sz, resolved_w, resolved_h,
                        resolved_date, resolved_lat, resolved_lon, resolved_desc, resolved_st,
                        resolved_err, resolved_proc, resolved_rem,
                        "BOTH", rec_a["id"], rec_b["id"]
                    )
                )
                new_id = cur_out.lastrowid
                map_a[rec_a["id"]] = new_id
                map_b[rec_b["id"]] = new_id

        # 3. Merge Tags
        print("Merging tags and photo_tag relations...")
        cur_a.execute("SELECT id, name FROM tags;")
        tags_a_dict = {row[0]: row[1] for row in cur_a.fetchall()}
        cur_b.execute("SELECT id, name FROM tags;")
        tags_b_dict = {row[0]: row[1] for row in cur_b.fetchall()}

        unified_tag_names = sorted(list(set(tags_a_dict.values()) | set(tags_b_dict.values())))
        tag_id_map: Dict[str, int] = {}
        for tag_name in unified_tag_names:
            cur_out.execute("INSERT INTO tags (name) VALUES (?);", (tag_name,))
            tag_id_map[tag_name] = cur_out.lastrowid

        # Read photo_tags from A
        cur_a.execute("SELECT photo_id, tag_id FROM photo_tags;")
        photo_tags_a: Set[Tuple[int, str]] = set()
        for p_id, t_id in cur_a.fetchall():
            if p_id in map_a and t_id in tags_a_dict:
                photo_tags_a.add((map_a[p_id], tags_a_dict[t_id]))

        # Read photo_tags from B
        cur_b.execute("SELECT photo_id, tag_id FROM photo_tags;")
        photo_tags_b: Set[Tuple[int, str]] = set()
        for p_id, t_id in cur_b.fetchall():
            if p_id in map_b and t_id in tags_b_dict:
                photo_tags_b.add((map_b[p_id], tags_b_dict[t_id]))

        all_photo_tags = photo_tags_a | photo_tags_b
        for new_p_id, t_name in all_photo_tags:
            in_tag_a = (new_p_id, t_name) in photo_tags_a
            in_tag_b = (new_p_id, t_name) in photo_tags_b
            tag_origin = "BOTH" if (in_tag_a and in_tag_b) else ("A" if in_tag_a else "B")

            cur_out.execute(
                "INSERT INTO photo_tags (photo_id, tag_id, tag_origin) VALUES (?, ?, ?);",
                (new_p_id, tag_id_map[t_name], tag_origin)
            )

        # 4. Merge Users, Settings, Tag Permissions
        print("Merging users, settings, and permissions...")
        # Settings: from A, then B if missing
        cur_a.execute("SELECT key, value FROM settings;")
        for k, v in cur_a.fetchall():
            cur_out.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);", (k, v))
        cur_b.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings';")
        if cur_b.fetchone():
            cur_b.execute("SELECT key, value FROM settings;")
            for k, v in cur_b.fetchall():
                cur_out.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", (k, v))

        # Tag permissions: from A, then B
        cur_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tag_permissions';")
        if cur_a.fetchone():
            cur_a.execute("SELECT tag_name, min_access_level, restricted_to_role FROM tag_permissions;")
            for tn, lvl, rol in cur_a.fetchall():
                cur_out.execute("INSERT OR REPLACE INTO tag_permissions (tag_name, min_access_level, restricted_to_role) VALUES (?, ?, ?);", (tn, lvl, rol))

        # Users and User Settings
        user_id_map: Dict[int, int] = {}
        cur_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
        if cur_a.fetchone():
            cur_a.execute("SELECT id, username, password_hash, role, access_level, created_at, last_login FROM users;")
            for uid, uname, phash, urole, uacc, ucreated, ulogin in cur_a.fetchall():
                cur_out.execute(
                    "INSERT INTO users (username, password_hash, role, access_level, created_at, last_login) VALUES (?, ?, ?, ?, ?, ?);",
                    (uname, phash, urole, uacc, ucreated, ulogin)
                )
                user_id_map[uid] = cur_out.lastrowid

            # User settings from A
            cur_a.execute("SELECT user_id, key, value FROM user_settings;")
            for uid, k, v in cur_a.fetchall():
                if uid in user_id_map:
                    cur_out.execute("INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?);", (user_id_map[uid], k, v))

        # 5. Merge Audit Logs
        print("Migrating and re-mapping audit logs...")
        cur_a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log';")
        if cur_a.fetchone():
            cur_a.execute("SELECT timestamp, user_id, username, action, photo_id, photo_filename, details FROM audit_log;")
            for ts, uid, uname, act, pid, pfn, det in cur_a.fetchall():
                new_pid = map_a.get(pid) if pid else None
                new_uid = user_id_map.get(uid) if uid else None
                cur_out.execute(
                    "INSERT INTO audit_log (timestamp, user_id, username, action, photo_id, photo_filename, details) VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (ts, new_uid, uname, act, new_pid, pfn, det)
                )

        conn_out.commit()
        # Reset journal mode to standard WAL or DELETE
        conn_out.execute("PRAGMA journal_mode = WAL;")
        conn_out.execute("PRAGMA synchronous = NORMAL;")
        print(f"Successfully generated merged catalog: {out_db_path}")
        print(f"Total time elapsed: {time.time() - start_time:.2f} seconds.")

    finally:
        conn_out.close()
        conn_a.close()
        conn_b.close()


def main():
    parser = argparse.ArgumentParser(description="Safe, Non-Destructive Photo Database Merger")
    parser.add_argument("db_a", type=Path, help="Path to primary database (Catalog A)")
    parser.add_argument("db_b", type=Path, help="Path to secondary database (Catalog B)")
    parser.add_argument("--out", type=Path, default=None, help="Path for output merged database (e.g. photos-merged.db)")
    parser.add_argument("--dry-run", action="store_true", help="Perform pre-merge inspection & statistics without writing any files")
    parser.add_argument("--force", action="store_true", help="Overwrite output database if it already exists")

    args = parser.parse_args()

    if not args.db_a.exists():
        sys.exit(f"Error: Catalog A does not exist: {args.db_a}")
    if not args.db_b.exists():
        sys.exit(f"Error: Catalog B does not exist: {args.db_b}")

    stats = analyze_catalogs(args.db_a, args.db_b)
    print_analysis_report(stats)

    if args.dry_run:
        print("\nDry-run complete. No files created or modified.")
        return

    if not args.out:
        print("\nSpecify --out <path> to proceed with creating the merged database.")
        return

    if args.out.exists() and args.force:
        print(f"Removing existing file at {args.out} (--force)...")
        args.out.unlink()

    execute_merge(args.db_a, args.db_b, args.out)


if __name__ == "__main__":
    main()
