#!/usr/bin/env python3
"""
test_web_routes.py - Integration tests for Web UI database management routes:
- Database switching
- Pre-merge dry-run analysis
- Database merging
- Database un-merging
- Database backups and snapshot creation
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as photo_app
from db_versioning import detect_schema_version, VERSION_AUTH_AUDIT, VERSION_PROVENANCE


class TestWebDatabaseRoutes(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_web_db_"))
        self.db_a_path = self.test_dir / "catalog_a.db"
        self.db_b_path = self.test_dir / "catalog_b.db"
        self._setup_databases()

        # Configure Flask app for testing
        photo_app.app.config["TESTING"] = True
        photo_app.app.config["SECRET_KEY"] = "test-secret-key"
        photo_app.DB_PATH = str(self.db_a_path)
        photo_app.DEFAULT_BACKUP_DIR = self.test_dir / "backups"
        photo_app.DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self.client = photo_app.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_databases(self):
        # Database A (v2 with admin user)
        conn_a = sqlite3.connect(str(self.db_a_path))
        conn_a.executescript("""
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
            CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
            CREATE TABLE photo_tags (photo_id INTEGER, tag_id INTEGER, PRIMARY KEY (photo_id, tag_id));
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT, role TEXT, access_level INTEGER, created_at TEXT, last_login TEXT);
            CREATE TABLE user_settings (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key));
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, user_id INTEGER, username TEXT, action TEXT, photo_id INTEGER, photo_filename TEXT, details TEXT);
            CREATE TABLE tag_permissions (id INTEGER PRIMARY KEY AUTOINCREMENT, tag_name TEXT UNIQUE, min_access_level INTEGER, restricted_to_role TEXT);
        """)
        conn_a.execute("INSERT INTO photos (filepath, filename, description) VALUES ('/img/a.jpg', 'a.jpg', 'Desc A');")
        conn_a.execute("INSERT INTO users (id, username, password_hash, role, access_level, created_at) VALUES (1, 'admin', 'hash', 'admin', 99, '2026-01-01');")
        conn_a.execute("INSERT INTO users (id, username, password_hash, role, access_level, created_at) VALUES (2, 'regular_user', 'hash', 'user', 1, '2026-01-01');")
        conn_a.commit()
        conn_a.close()

        # Database B (v1)
        conn_b = sqlite3.connect(str(self.db_b_path))
        conn_b.executescript("""
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
                processed_at TEXT
            );
            CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
            CREATE TABLE photo_tags (photo_id INTEGER, tag_id INTEGER, PRIMARY KEY (photo_id, tag_id));
        """)
        conn_b.execute("INSERT INTO photos (filepath, filename, description) VALUES ('/img/b.jpg', 'b.jpg', 'Desc B');")
        conn_b.commit()
        conn_b.close()

    def _login_as_admin(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "admin"

    def test_settings_page_renders_database_info(self):
        self._login_as_admin()
        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"catalog_a.db", res.data)
        self.assertIn(b"Schema Version", res.data)
        self.assertIn(b"Database Backups", res.data)

    def test_settings_tabs_admin_view(self):
        self._login_as_admin()
        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        # Check nav bar
        self.assertIn(b'class="settings-tabs-nav"', res.data)
        # Check tab buttons
        self.assertIn(b'data-tab="general"', res.data)
        self.assertIn(b'data-tab="database"', res.data)
        self.assertIn(b'data-tab="users"', res.data)
        # Check tab panels
        self.assertIn(b'id="panel-general"', res.data)
        self.assertIn(b'id="panel-database"', res.data)
        self.assertIn(b'id="panel-users"', res.data)

    def test_settings_tabs_non_admin_view(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 2
            sess["username"] = "regular_user"
            sess["role"] = "user"
        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        # General & Database are present
        self.assertIn(b'data-tab="general"', res.data)
        self.assertIn(b'data-tab="database"', res.data)
        # User management is hidden for non-admin
        self.assertNotIn(b'data-tab="users"', res.data)
        self.assertNotIn(b'id="panel-users"', res.data)

    def test_database_action_redirect_hash(self):
        self._login_as_admin()
        res = self.client.post("/settings/backup/create", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].endswith("#database"))

    def test_user_action_redirect_hash(self):
        self._login_as_admin()
        res = self.client.post(
            "/admin/users/create",
            data={"username": "newtester", "password": "pass1234", "role": "user"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].endswith("#users"))

    def test_switch_database_route(self):
        self._login_as_admin()
        res = self.client.post("/settings/database/switch", data={"target_db": str(self.db_b_path)}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(photo_app.DB_PATH, str(self.db_b_path))

    def test_analyze_and_merge_routes(self):
        self._login_as_admin()
        # 1. Analyze
        res = self.client.post(
            "/settings/database/analyze",
            data={"db_a": str(self.db_a_path), "db_b": str(self.db_b_path)},
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Pre-Merge Analysis Report", res.data)

        # 2. Execute Merge
        out_name = "test-merged-web.db"
        res = self.client.post(
            "/settings/database/merge",
            data={"db_a": str(self.db_a_path), "db_b": str(self.db_b_path), "output_name": out_name},
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        merged_file = Path(".") / out_name
        self.assertTrue(merged_file.exists())

        # Verify merged DB has Schema v3
        ver = detect_schema_version(merged_file)
        self.assertEqual(ver, VERSION_PROVENANCE)
        for p in [merged_file, Path(str(merged_file) + "-wal"), Path(str(merged_file) + "-shm")]:
            if p.exists():
                p.unlink()

    def test_backup_create_route(self):
        self._login_as_admin()
        res = self.client.post("/settings/backup/create", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        backups = list(photo_app.DEFAULT_BACKUP_DIR.glob("*.db"))
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
