#!/usr/bin/env python3
"""
test_merge_unmerge.py - End-to-end unit and verification test for:
1. Backup creation and retention
2. Schema versioning and auto-detection
3. Safe merge with conflict snapshots and tag union
4. Un-merge with rollback (pre-merge value restoration)
5. Un-merge with forward (post-merge tag and edit propagation)
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backup_utils import backup_database, list_backups, prune_backups
from db_versioning import detect_schema_version, migrate_database, VERSION_LEGACY, VERSION_AUTH_AUDIT, VERSION_PROVENANCE
from merge_catalogs import execute_merge, analyze_catalogs
from unmerge_catalogs import unmerge_database


class TestMergeUnmerge(unittest.TestCase):

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_db_merge_"))
        self.db_a_path = self.test_dir / "catalog_a.db"
        self.db_b_path = self.test_dir / "catalog_b.db"
        self.merged_path = self.test_dir / "catalog_merged.db"
        self.backup_dir = self.test_dir / "backups"

        self._create_sample_databases()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_databases(self):
        # Database A (v2 with auth/audit)
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

        # Add data to A:
        # Photo 1: Only in A
        # Photo 2: Shared in both, A has removal=0, desc="A desc"
        conn_a.execute("INSERT INTO photos (filepath, filename, description, marked_for_removal, status) VALUES ('/photos/p1.jpg', 'p1.jpg', 'Photo 1 in A', 0, 'done');")
        conn_a.execute("INSERT INTO photos (filepath, filename, description, marked_for_removal, status) VALUES ('/photos/p2.jpg', 'p2.jpg', 'Shared Photo Description from A', 0, 'done');")

        # Tags in A
        conn_a.execute("INSERT INTO tags (name) VALUES ('nature');") # id 1
        conn_a.execute("INSERT INTO tags (name) VALUES ('vacation');") # id 2
        conn_a.execute("INSERT INTO photo_tags VALUES (1, 1);") # p1 has 'nature'
        conn_a.execute("INSERT INTO photo_tags VALUES (2, 2);") # p2 has 'vacation'

        # Admin user in A
        conn_a.execute("INSERT INTO users (username, password_hash, role, access_level, created_at) VALUES ('admin', 'hash123', 'admin', 99, '2026-01-01');")
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

        # Add data to B:
        # Photo 2: Shared in both, B has desc="Short B", status="done"
        # Photo 3: Only in B
        conn_b.execute("INSERT INTO photos (filepath, filename, description, status) VALUES ('/photos/p2.jpg', 'p2.jpg', 'Short B', 'done');")
        conn_b.execute("INSERT INTO photos (filepath, filename, description, status) VALUES ('/photos/p3.jpg', 'p3.jpg', 'Photo 3 in B', 'done');")

        # Tags in B
        conn_b.execute("INSERT INTO tags (name) VALUES ('family');") # id 1
        conn_b.execute("INSERT INTO tags (name) VALUES ('vacation');") # id 2
        conn_b.execute("INSERT INTO photo_tags VALUES (1, 1);") # p2 has 'family'
        conn_b.execute("INSERT INTO photo_tags VALUES (1, 2);") # p2 has 'vacation'
        conn_b.execute("INSERT INTO photo_tags VALUES (2, 1);") # p3 has 'family'
        conn_b.commit()
        conn_b.close()

    def test_version_detection(self):
        ver_a = detect_schema_version(self.db_a_path)
        ver_b = detect_schema_version(self.db_b_path)
        self.assertEqual(ver_a, VERSION_AUTH_AUDIT)
        self.assertEqual(ver_b, VERSION_LEGACY)

    def test_backup_and_retention(self):
        b1 = backup_database(self.db_a_path, backup_dir=self.backup_dir, max_backups=2)
        b2 = backup_database(self.db_a_path, backup_dir=self.backup_dir, max_backups=2)
        b3 = backup_database(self.db_a_path, backup_dir=self.backup_dir, max_backups=2)

        backups = list_backups(self.backup_dir)
        self.assertEqual(len(backups), 2)
        self.assertTrue(Path(backups[0]["path"]).exists())

    def test_merge_and_lineage(self):
        # Perform dry run analysis
        stats = analyze_catalogs(self.db_a_path, self.db_b_path)
        self.assertEqual(stats["total_a"], 2)
        self.assertEqual(stats["total_b"], 2)
        self.assertEqual(stats["only_a"], 1)
        self.assertEqual(stats["only_b"], 1)
        self.assertEqual(stats["shared"], 1)
        self.assertEqual(stats["total_merged_estimate"], 3)

        # Execute actual merge
        execute_merge(self.db_a_path, self.db_b_path, self.merged_path)
        self.assertTrue(self.merged_path.exists())

        ver_merged = detect_schema_version(self.merged_path)
        self.assertEqual(ver_merged, VERSION_PROVENANCE)

        conn_m = sqlite3.connect(str(self.merged_path))
        cur = conn_m.cursor()

        # Verify 3 photos
        cur.execute("SELECT count(*) FROM photos;")
        self.assertEqual(cur.fetchone()[0], 3)

        # Verify origin tracking
        cur.execute("SELECT filepath, origin_sources, origin_id_a, origin_id_b, description FROM photos ORDER BY filepath;")
        rows = cur.fetchall()
        # p1: only in A
        self.assertEqual(rows[0][0], "/photos/p1.jpg")
        self.assertEqual(rows[0][1], "A")
        self.assertEqual(rows[0][2], 1)
        self.assertIsNone(rows[0][3])

        # p2: shared (BOTH)
        self.assertEqual(rows[1][0], "/photos/p2.jpg")
        self.assertEqual(rows[1][1], "BOTH")
        self.assertEqual(rows[1][2], 2)
        self.assertEqual(rows[1][3], 1)
        # Check description resolved to longer
        self.assertEqual(rows[1][4], "Shared Photo Description from A")

        # p3: only in B
        self.assertEqual(rows[2][0], "/photos/p3.jpg")
        self.assertEqual(rows[2][1], "B")
        self.assertIsNone(rows[2][2])
        self.assertEqual(rows[2][3], 2)

        # Verify conflict snapshot was created for p2 description
        cur.execute("SELECT filepath, field_name, val_in_db_a, val_in_db_b, resolved_val FROM merge_conflict_snapshots;")
        conflicts = cur.fetchall()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][0], "/photos/p2.jpg")
        self.assertEqual(conflicts[0][1], "description")
        self.assertEqual(conflicts[0][2], "Shared Photo Description from A")
        self.assertEqual(conflicts[0][3], "Short B")

        # Verify tag union and tag lineage on shared p2
        # p2 should have both 'vacation' (from A -> BOTH if in B, and B had vacation too) and 'family' (from B)
        cur.execute("""
            SELECT t.name, pt.tag_origin
            FROM photo_tags pt
            JOIN tags t ON pt.tag_id = t.id
            JOIN photos p ON pt.photo_id = p.id
            WHERE p.filepath = '/photos/p2.jpg'
            ORDER BY t.name;
        """)
        p2_tags = cur.fetchall()
        # 'family' came only from B -> 'B'
        # 'vacation' came from both -> 'BOTH'
        self.assertEqual(p2_tags, [("family", "B"), ("vacation", "BOTH")])

        conn_m.close()

    def test_unmerge_rollback(self):
        execute_merge(self.db_a_path, self.db_b_path, self.merged_path)

        restored_a = self.test_dir / "restored_a.db"
        restored_b = self.test_dir / "restored_b.db"

        unmerge_database(self.merged_path, out_a_path=restored_a, out_b_path=restored_b, strategy="rollback")

        self.assertTrue(restored_a.exists())
        self.assertTrue(restored_b.exists())

        # Check restored A
        conn_ra = sqlite3.connect(str(restored_a))
        cur_ra = conn_ra.cursor()
        cur_ra.execute("SELECT id, filepath, description FROM photos ORDER BY id;")
        a_photos = cur_ra.fetchall()
        self.assertEqual(len(a_photos), 2)
        self.assertEqual(a_photos[0], (1, "/photos/p1.jpg", "Photo 1 in A"))
        self.assertEqual(a_photos[1], (2, "/photos/p2.jpg", "Shared Photo Description from A"))

        # Check restored B
        conn_rb = sqlite3.connect(str(restored_b))
        cur_rb = conn_rb.cursor()
        cur_rb.execute("SELECT id, filepath, description FROM photos ORDER BY id;")
        b_photos = cur_rb.fetchall()
        self.assertEqual(len(b_photos), 2)
        # p2 in B had original ID 1, description "Short B"
        self.assertEqual(b_photos[0], (1, "/photos/p2.jpg", "Short B"))
        # p3 in B had original ID 2, description "Photo 3 in B"
        self.assertEqual(b_photos[1], (2, "/photos/p3.jpg", "Photo 3 in B"))

        conn_ra.close()
        conn_rb.close()

    def test_unmerge_forward(self):
        execute_merge(self.db_a_path, self.db_b_path, self.merged_path)

        # Simulate web app adding a tag post-merge to p1 (A-only photo)
        conn_m = sqlite3.connect(str(self.merged_path))
        conn_m.execute("INSERT INTO tags (name) VALUES ('post_merge_tag');")
        tag_id = conn_m.execute("SELECT id FROM tags WHERE name = 'post_merge_tag';").fetchone()[0]
        p1_id = conn_m.execute("SELECT id FROM photos WHERE filepath = '/photos/p1.jpg';").fetchone()[0]
        conn_m.execute("INSERT INTO photo_tags (photo_id, tag_id, tag_origin) VALUES (?, ?, 'POST_MERGE');", (p1_id, tag_id))
        # Update description on p2
        conn_m.execute("UPDATE photos SET description = 'Updated in Merged Web App' WHERE filepath = '/photos/p2.jpg';")
        conn_m.commit()
        conn_m.close()

        restored_a = self.test_dir / "forward_a.db"
        restored_b = self.test_dir / "forward_b.db"

        unmerge_database(self.merged_path, out_a_path=restored_a, out_b_path=restored_b, strategy="forward")

        # In forward mode, p2 in both A and B should retain the updated description
        conn_ra = sqlite3.connect(str(restored_a))
        cur_ra = conn_ra.cursor()
        cur_ra.execute("SELECT description FROM photos WHERE filepath = '/photos/p2.jpg';")
        self.assertEqual(cur_ra.fetchone()[0], "Updated in Merged Web App")

        # p1 in restored A should have the post_merge_tag
        cur_ra.execute("""
            SELECT t.name FROM photo_tags pt
            JOIN tags t ON pt.tag_id = t.id
            JOIN photos p ON pt.photo_id = p.id
            WHERE p.filepath = '/photos/p1.jpg';
        """)
        tags_p1 = [r[0] for r in cur_ra.fetchall()]
        self.assertIn("post_merge_tag", tags_p1)
        self.assertIn("nature", tags_p1)

        conn_ra.close()



if __name__ == "__main__":
    unittest.main()
