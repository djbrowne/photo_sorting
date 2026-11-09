#!/usr/bin/env python3
"""
test_catalog_photos.py - Tests that catalog_photos.py creates and maintains
a database that is 100% compatible with Schema v3.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import catalog_photos
from db_versioning import detect_schema_version, check_compatibility, VERSION_PROVENANCE


class TestCatalogPhotosSchemaCompatibility(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_catalog_"))
        self.db_path = self.test_dir / "test_photos.db"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_init_db_creates_schema_v3(self):
        conn = catalog_photos.init_db(self.db_path)
        conn.close()

        # Check version via db_versioning
        version = detect_schema_version(self.db_path)
        self.assertEqual(version, VERSION_PROVENANCE)

        is_compat, ver, msg = check_compatibility(self.db_path)
        self.assertTrue(is_compat)
        self.assertEqual(ver, VERSION_PROVENANCE)

        # Inspect tables
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r[0] for r in cur.fetchall()}
        required_tables = {
            "photos", "tags", "photo_tags", "settings", "users",
            "user_settings", "audit_log", "tag_permissions",
            "merge_manifest", "merge_conflict_snapshots", "schema_migrations"
        }
        self.assertTrue(required_tables.issubset(tables))

        # Inspect photo columns
        cur.execute("PRAGMA table_info(photos);")
        photo_cols = {r[1] for r in cur.fetchall()}
        self.assertIn("marked_for_removal", photo_cols)
        self.assertIn("origin_sources", photo_cols)
        self.assertIn("origin_id_a", photo_cols)
        self.assertIn("origin_id_b", photo_cols)

        # Inspect photo_tags columns
        cur.execute("PRAGMA table_info(photo_tags);")
        pt_cols = {r[1] for r in cur.fetchall()}
        self.assertIn("tag_origin", pt_cols)

        conn.close()

    def test_save_result_and_preservation(self):
        conn = catalog_photos.init_db(self.db_path)
        dummy_file = self.test_dir / "sample.jpg"
        dummy_file.write_text("fake image")

        meta = {
            "size_bytes": 1024,
            "width": 800,
            "height": 600,
            "date_taken": "2026-05-15 10:00:00",
            "gps_lat": 47.5,
            "gps_lon": -52.7,
        }
        catalog_photos.save_result(conn, dummy_file, meta, ["nature", "birds"], "A scenic view", "done")

        # Verify photo inserted with defaults
        cur = conn.cursor()
        cur.execute("SELECT marked_for_removal, origin_sources FROM photos WHERE filename = 'sample.jpg';")
        row = cur.fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "ORIGINAL")

        # Verify tag_origin
        cur.execute("""
            SELECT t.name, pt.tag_origin FROM photo_tags pt
            JOIN tags t ON pt.tag_id = t.id
            ORDER BY t.name;
        """)
        tag_rows = cur.fetchall()
        self.assertEqual(tag_rows, [("birds", "ORIGINAL"), ("nature", "ORIGINAL")])

        # Simulate user marking photo for removal in web app
        conn.execute("UPDATE photos SET marked_for_removal = 1 WHERE filename = 'sample.jpg';")
        conn.commit()

        # Re-save with updated tags/description
        catalog_photos.save_result(conn, dummy_file, meta, ["nature", "puffins"], "Updated description", "done")

        # Verify marked_for_removal was PRESERVED
        cur.execute("SELECT marked_for_removal, description FROM photos WHERE filename = 'sample.jpg';")
        row2 = cur.fetchone()
        self.assertEqual(row2[0], 1)
        self.assertEqual(row2[1], "Updated description")

        conn.close()

    def test_upgrade_legacy_database(self):
        # Create a legacy v1 database
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
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
        conn.execute("INSERT INTO photos (filepath, filename) VALUES ('/old/p.jpg', 'p.jpg');")
        conn.commit()
        conn.close()

        # Run init_db on legacy database
        conn2 = catalog_photos.init_db(self.db_path)
        conn2.close()

        # Verify it upgraded to v3 and preserved the existing photo
        version = detect_schema_version(self.db_path)
        self.assertEqual(version, VERSION_PROVENANCE)

        conn3 = sqlite3.connect(str(self.db_path))
        cur = conn3.cursor()
        cur.execute("SELECT filepath, marked_for_removal, origin_sources FROM photos WHERE filename = 'p.jpg';")
        row = cur.fetchone()
        self.assertEqual(row[0], "/old/p.jpg")
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], "ORIGINAL")
        conn3.close()


if __name__ == "__main__":
    unittest.main()
