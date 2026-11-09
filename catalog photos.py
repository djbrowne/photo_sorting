#!/usr/bin/env python3
"""
catalog_photos.py -- walk directories of photos, pull file + EXIF metadata,
categorize each photo's content with a local Ollama vision model, and store
everything in a SQLite database.

SETUP (one-time)
-----------------
    pip install pillow requests
    # only needed if your library has iPhone photos:
    pip install pillow-heif

    # pull a vision model in Ollama if you haven't already, e.g. one of:
    ollama pull moondream        # small + fast -- good first thing to try on older/limited GPUs
    ollama pull llava            # bigger, generally more accurate, slower

USAGE
-----
    # test run on the first 50 new photos, to sanity-check speed & tag quality
    python3 catalog_photos.py /mnt/drive1 /mnt/drive2 --limit 50

    # the real run -- just re-run the same command; already-processed files
    # (status='done') are skipped automatically, so it's safe to stop with
    # Ctrl-C and resume any time by running the exact same command again.
    python3 catalog_photos.py /mnt/drive1 /mnt/drive2

    # if Ollama is reachable somewhere other than the default localhost:11434
    # (e.g. a different port mapped from your ROCm docker container):
    python3 catalog_photos.py /mnt/drive1 --host http://localhost:PORT

Each photo becomes one row in the `photos` table (filepath, filename, size,
width/height, date_taken, gps_lat/gps_lon, description, status) plus rows in
`tags` / `photo_tags` for its category tags -- so "find every bird photo" is
just a join (see the example queries at the bottom of this file).
"""

import argparse
import base64
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ExifTags

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC/HEIF files will fail with a clear error unless pillow-heif is installed

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".heif"}

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "moondream"
REQUEST_TIMEOUT = 300  # seconds -- generous, since older/limited GPUs can be slow per image

ORIG_PROMPT = """Look at this photo and respond with ONLY a JSON object, no other text, in this exact format:
{"tags": ["tag1", "tag2"], "description": "one plain sentence describing the photo"}

Use short, lowercase, singular words for the main subjects in the photo (for example: bird, person, motorcycle, plant, dog, car, building, food, landscape, water). Include between 1 and 4 tags, ordered by how prominent each subject is. Keep the description to a single factual sentence."""

PROMPT = """Examine this photo and respond with ONLY a JSON object, no other text, in this exact format:
{"tags": ["tag1", "tag2"], "description": "Maximum three sentences describing the photo"}

Use short, lowercase, singular words for the main subjects in the photo 
(for example: bird, person, motorcycle, plant, dog, car, building, food, landscape, water).
Include between 1 and 4 tags, ordered by how prominent each subject is. Keep the description to a 
short, 3 sentence, factual sentence.  If you can identify the type of bird in the photo include the 
bird name in the description"""

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def init_db(db_path):
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS photos (
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
            status TEXT DEFAULT 'pending',   -- 'done' or 'error'
            error_message TEXT,
            processed_at TEXT,
            marked_for_removal INTEGER NOT NULL DEFAULT 0,
            origin_sources TEXT NOT NULL DEFAULT 'ORIGINAL',
            origin_id_a INTEGER,
            origin_id_b INTEGER
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS photo_tags (
            photo_id INTEGER NOT NULL REFERENCES photos(id),
            tag_id INTEGER NOT NULL REFERENCES tags(id),
            tag_origin TEXT NOT NULL DEFAULT 'ORIGINAL',
            PRIMARY KEY (photo_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            access_level INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
            photo_filename TEXT,
            details TEXT
        );

        CREATE TABLE IF NOT EXISTS tag_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_name TEXT UNIQUE NOT NULL,
            min_access_level INTEGER NOT NULL DEFAULT 1,
            restricted_to_role TEXT
        );

        CREATE TABLE IF NOT EXISTS merge_manifest (
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

        CREATE TABLE IF NOT EXISTS merge_conflict_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manifest_id INTEGER REFERENCES merge_manifest(id),
            filepath TEXT NOT NULL,
            field_name TEXT NOT NULL,
            val_in_db_a TEXT,
            val_in_db_b TEXT,
            resolved_val TEXT
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL,
            min_app_version TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_photos_filepath ON photos(filepath);
        CREATE INDEX IF NOT EXISTS idx_audit_photo ON audit_log(photo_id);
        CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_conflict_filepath ON merge_conflict_snapshots(filepath);
    """)

    # When opening an existing database that was created with an older schema, migrate missing columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if "marked_for_removal" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN marked_for_removal INTEGER NOT NULL DEFAULT 0;")
    if "origin_sources" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN origin_sources TEXT NOT NULL DEFAULT 'ORIGINAL';")
    if "origin_id_a" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN origin_id_a INTEGER;")
    if "origin_id_b" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN origin_id_b INTEGER;")

    pt_cols = {r[1] for r in conn.execute("PRAGMA table_info(photo_tags)").fetchall()}
    if "tag_origin" not in pt_cols:
        conn.execute("ALTER TABLE photo_tags ADD COLUMN tag_origin TEXT NOT NULL DEFAULT 'ORIGINAL';")

    # Set and record PRAGMA user_version = 3 (CURRENT_SCHEMA_VERSION)
    cur = conn.cursor()
    cur.execute("PRAGMA user_version;")
    row = cur.fetchone()
    current_uv = row[0] if row else 0
    if current_uv < 3:
        conn.execute("PRAGMA user_version = 3;")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT OR REPLACE INTO schema_migrations (version, applied_at, description, min_app_version)
               VALUES (3, ?, 'Schema v3 initialized/verified by catalog_photos.py', '2.0');""",
            (now_str,)
        )

    conn.commit()
    return conn


def already_done(conn, filepath):
    row = conn.execute("SELECT status FROM photos WHERE filepath = ?", (str(filepath),)).fetchone()
    return row is not None and row[0] == "done"


def get_or_create_tag(conn, name):
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]


def save_result(conn, filepath, meta, tags, description, status, error_message=None):
    conn.execute(
        """
        INSERT INTO photos (filepath, filename, size_bytes, width, height, date_taken,
                             gps_lat, gps_lon, description, status, error_message, processed_at,
                             marked_for_removal, origin_sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 0, 'ORIGINAL')
        ON CONFLICT(filepath) DO UPDATE SET
            size_bytes=excluded.size_bytes, width=excluded.width, height=excluded.height,
            date_taken=excluded.date_taken, gps_lat=excluded.gps_lat, gps_lon=excluded.gps_lon,
            description=excluded.description, status=excluded.status,
            error_message=excluded.error_message, processed_at=excluded.processed_at
        """,
        (str(filepath), filepath.name, meta.get("size_bytes"), meta.get("width"), meta.get("height"),
         meta.get("date_taken"), meta.get("gps_lat"), meta.get("gps_lon"),
         description, status, error_message),
    )
    photo_id = conn.execute("SELECT id FROM photos WHERE filepath = ?", (str(filepath),)).fetchone()[0]

    pt_cols = {r[1] for r in conn.execute("PRAGMA table_info(photo_tags)").fetchall()}
    has_tag_origin = "tag_origin" in pt_cols

    if has_tag_origin:
        conn.execute("DELETE FROM photo_tags WHERE photo_id = ? AND tag_origin != 'POST_MERGE'", (photo_id,))
        for tag in tags:
            tag_id = get_or_create_tag(conn, tag)
            conn.execute(
                "INSERT OR IGNORE INTO photo_tags (photo_id, tag_id, tag_origin) VALUES (?, ?, 'ORIGINAL')",
                (photo_id, tag_id),
            )
    else:
        conn.execute("DELETE FROM photo_tags WHERE photo_id = ?", (photo_id,))
        for tag in tags:
            tag_id = get_or_create_tag(conn, tag)
            conn.execute("INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?)", (photo_id, tag_id))
    conn.commit()


# --------------------------------------------------------------------------
# Filesystem + EXIF
# --------------------------------------------------------------------------

def find_photos(roots):
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield path


def _dms_to_degrees(dms):
    if not dms:
        return None
    d, m, s = dms
    return float(d) + float(m) / 60 + float(s) / 3600


def get_exif_data(filepath):
    """Best-effort metadata pull. Never raises -- missing/unreadable EXIF just means fewer fields."""
    result = {}
    try:
        img = Image.open(filepath)
        result["width"], result["height"] = img.size
        exif = img.getexif()
        if exif:
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            date_str = exif_ifd.get(36867) or exif.get(306)  # DateTimeOriginal, else DateTime
            if date_str:
                date_str = str(date_str)
                if len(date_str) >= 10 and date_str[4] == ":" and date_str[7] == ":":
                    date_str = date_str[:4] + "-" + date_str[5:7] + "-" + date_str[8:]
                result["date_taken"] = date_str

            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
            if gps_ifd:
                gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                lat = _dms_to_degrees(gps.get("GPSLatitude"))
                lon = _dms_to_degrees(gps.get("GPSLongitude"))
                if lat is not None and lon is not None:
                    if gps.get("GPSLatitudeRef") == "S":
                        lat = -lat
                    if gps.get("GPSLongitudeRef") == "W":
                        lon = -lon
                    result["gps_lat"] = lat
                    result["gps_lon"] = lon
    except Exception:
        pass  # leave whatever fields we already have; the photo still gets cataloged
    return result


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------

def categorize_image(filepath, host, model):
    with open(filepath, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    resp = requests.post(
        f"{host}/api/generate",
        json={
            "model": model,
            "prompt": PROMPT,
            "images": [img_b64],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.2},
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    raw = resp.json()["response"]
    parsed = json.loads(raw)  # raises if the model didn't return valid JSON -- caller logs it as an error
    tags = [str(t).strip().lower() for t in parsed.get("tags", []) if str(t).strip()]
    description = str(parsed.get("description", "")).strip()
    return tags, description


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Catalog photos with a local Ollama vision model.")
    parser.add_argument("roots", nargs="+", help="One or more directories to scan (recursively)")
    parser.add_argument("--db", default="photos.db", help="SQLite database path (default: photos.db)")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama API base URL (default: {DEFAULT_HOST})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama vision model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N newly-processed photos (for test runs)")
    args = parser.parse_args()

    for root in args.roots:
        if not Path(root).is_dir():
            print(f"Not a directory, skipping: {root}", file=sys.stderr)

    conn = init_db(args.db)
    processed = skipped = errors = 0

    for filepath in find_photos(args.roots):
        if already_done(conn, filepath):
            skipped += 1
            continue

        if args.limit is not None and processed >= args.limit:
            print(f"\nReached --limit {args.limit}, stopping. Re-run the same command later to continue.")
            break

        meta = {"size_bytes": filepath.stat().st_size}
        meta.update(get_exif_data(filepath))

        try:
            tags, description = categorize_image(filepath, args.host, args.model)
            save_result(conn, filepath, meta, tags, description, "done")
            processed += 1
            print(f"[{processed}] OK    {filepath.name}  ->  {tags}")
        except Exception as e:
            save_result(conn, filepath, meta, [], None, "error", str(e))
            errors += 1
            print(f"[{processed + errors}] FAIL  {filepath.name}  ->  {e}")

    print(f"\nDone this run. Newly processed: {processed}  Already done (skipped): {skipped}  Errors: {errors}")
    print(f"Database: {Path(args.db).resolve()}")
    conn.close()


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------
# Example queries once the database has some data in it
# (sqlite3 photos.db   then paste these in, or use any SQLite browser)
# --------------------------------------------------------------------------
#
# -- everything tagged as a bird
# SELECT p.filepath FROM photos p
# JOIN photo_tags pt ON p.id = pt.photo_id
# JOIN tags t ON pt.tag_id = t.id
# WHERE t.name = 'bird';
#
# -- most common tags overall
# SELECT t.name, COUNT(*) c FROM tags t
# JOIN photo_tags pt ON t.id = pt.tag_id
# GROUP BY t.name ORDER BY c DESC LIMIT 20;
#
# -- photos that failed, so you can see what went wrong
# SELECT filepath, error_message FROM photos WHERE status = 'error';
#
# -- photos with GPS data, roughly ordered by location
# SELECT filepath, gps_lat, gps_lon FROM photos
# WHERE gps_lat IS NOT NULL ORDER BY gps_lat, gps_lon;
