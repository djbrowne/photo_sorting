#!/usr/bin/env python3
"""
backup_utils.py - Safe, non-blocking SQLite database backup utility.
Uses Python's native SQLite Backup API to create consistent snapshots
even while the database is actively read or written by the web app.
"""

import os
import sys
import sqlite3
import argparse
import datetime
from pathlib import Path
from typing import List, Dict, Optional

DEFAULT_BACKUP_DIR = Path("/mnt/nfs/site/backups")
DEFAULT_RETENTION = 5


def ensure_backup_dir(backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    """Ensure the backup directory exists and has correct permissions."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def backup_database(
    source_db_path: str | Path,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    prefix: Optional[str] = None,
    max_backups: int = DEFAULT_RETENTION,
) -> Path:
    """
    Safely creates an online, non-blocking backup of the given SQLite database.
    Retains up to max_backups snapshots for this database.
    """
    src_path = Path(source_db_path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"Source database not found: {src_path}")

    target_dir = ensure_backup_dir(Path(backup_dir))
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    db_base = prefix or src_path.stem
    dest_filename = f"{db_base}_{timestamp}.db"
    dest_path = target_dir / dest_filename
    if dest_path.exists():
        timestamp_ms = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest_filename = f"{db_base}_{timestamp_ms}.db"
        dest_path = target_dir / dest_filename

    # Perform online SQLite backup via native backup API
    src_uri = f"file:{src_path}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True, timeout=30.0)
    dest_conn = sqlite3.connect(str(dest_path), timeout=30.0)

    try:
        with dest_conn:
            src_conn.backup(dest_conn, pages=100, sleep=0.01)
    finally:
        dest_conn.close()
        src_conn.close()

    # Apply retention pruning
    if max_backups > 0:
        prune_backups(backup_dir=target_dir, db_name=db_base, max_backups=max_backups)

    return dest_path


def list_backups(
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    db_name: Optional[str] = None,
) -> List[Dict]:
    """
    List existing database backups in order from newest to oldest.
    """
    target_dir = Path(backup_dir)
    if not target_dir.exists():
        return []

    backups = []
    pattern = "*.db"
    for p in target_dir.glob(pattern):
        if not p.is_file():
            continue
        if db_name and not p.name.startswith(db_name):
            continue

        stat = p.stat()
        mtime = datetime.datetime.fromtimestamp(stat.st_mtime)

        # Inspect user_version quickly
        version = None
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
            cur = conn.cursor()
            cur.execute("PRAGMA user_version;")
            row = cur.fetchone()
            if row:
                version = row[0]
            conn.close()
        except Exception:
            version = "unknown"

        backups.append({
            "path": str(p),
            "filename": p.name,
            "size_bytes": stat.st_size,
            "size_formatted": _format_size(stat.st_size),
            "created_at": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": stat.st_mtime,
            "version": version,
        })

    backups.sort(key=lambda x: x["timestamp"], reverse=True)
    return backups


def prune_backups(
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    db_name: Optional[str] = None,
    max_backups: int = DEFAULT_RETENTION,
) -> List[Path]:
    """
    Prune backups exceeding max_backups retention for the given database prefix.
    Returns list of deleted paths.
    """
    if max_backups <= 0:
        return []

    all_backups = list_backups(backup_dir=backup_dir, db_name=db_name)
    deleted = []
    if len(all_backups) > max_backups:
        for extra in all_backups[max_backups:]:
            path_to_delete = Path(extra["path"])
            try:
                path_to_delete.unlink()
                deleted.append(path_to_delete)
            except OSError as e:
                print(f"Warning: could not prune backup {path_to_delete}: {e}", file=sys.stderr)

    return deleted


def _format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def main():
    parser = argparse.ArgumentParser(description="SQLite Database Online Backup Utility")
    parser.add_argument("--db", type=str, help="Path to database to back up")
    parser.add_argument("--dest", type=str, default=str(DEFAULT_BACKUP_DIR), help="Destination backup directory")
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION, help="Number of backups to keep (default: 5)")
    parser.add_argument("--list", action="store_true", help="List available backups")
    parser.add_argument("--prefix", type=str, default=None, help="Filter prefix for list/prune")

    args = parser.parse_args()

    if args.list:
        backups = list_backups(backup_dir=args.dest, db_name=args.prefix)
        if not backups:
            print(f"No backups found in {args.dest}")
            return
        print(f"{'Filename':<40} {'Size':<10} {'Version':<8} {'Created At'}")
        print("-" * 75)
        for b in backups:
            print(f"{b['filename']:<40} {b['size_formatted']:<10} {str(b['version']):<8} {b['created_at']}")
        return

    if not args.db:
        parser.error("Must specify --db to perform backup or --list to view existing backups.")

    print(f"Backing up database: {args.db} -> {args.dest}")
    dest_path = backup_database(args.db, backup_dir=args.dest, prefix=args.prefix, max_backups=args.retention)
    print(f"Backup created successfully: {dest_path}")


if __name__ == "__main__":
    main()
