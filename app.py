#!/usr/bin/env python3
"""
photo_site/app.py -- local web viewer/editor for photos.db (built by catalog_photos.py)

Browse cataloged photos in a grid, mark photos for removal (a flag only --
nothing is ever deleted, on disk or in the database), add or remove tags,
and edit descriptions. No login -- built for a trusted internal network.

SETUP
-----
    pip install flask pillow
    # only if your library has iPhone photos and catalog_photos.py needed it too:
    pip install pillow-heif

RUN
---
    python3 app.py --db /path/to/photos.db 

    Then from any device on your LAN:  http://<this-machine-ip>:8080

    Options:
      --db PATH      path to photos.db (default: photos.db in the current directory)
      --host HOST    bind address (default: 0.0.0.0 -- reachable from your LAN)
      --port PORT    port (default: 8080)
"""

import argparse
import json
import sqlite3
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_file, abort, g, redirect, url_for
from PIL import Image, ImageDraw

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

app = Flask(__name__)

DB_PATH = "photos.db"           # set from --db at startup
THUMB_DIR = Path("thumbnails")  # cache dir for generated thumbnails, created on demand
THUMB_SIZE = (320, 320)
PER_PAGE = 50

DEFAULT_SETTINGS = {
    "all_tags_count": "1",
    "show_zero_tags": "0",
    "count_style": "column",
    "dark_mode": "0",
    "per_page": "50",
}


# --------------------------------------------------------------------------
# Database & Settings
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_settings():
    db = get_db()
    s = dict(DEFAULT_SETTINGS)
    for row in db.execute("SELECT key, value FROM settings"):
        s[row["key"]] = row["value"]
    return s


def save_settings(new_settings):
    db = get_db()
    for k in DEFAULT_SETTINGS:
        if k in new_settings:
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(new_settings[k])))
    db.commit()


def migrate(db_path):
    """catalog_photos.py's schema has no marked_for_removal column -- add it once, if missing."""
    conn = sqlite3.connect(db_path)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(photos)")]
    if "marked_for_removal" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN marked_for_removal INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        print("Migrated photos.db: added marked_for_removal column.")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()


@app.template_filter("filesize")
def filesize_filter(n):
    if not n:
        return "?"
    return f"{n / 1_048_576:.1f} MB"


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@app.route("/")
def index():
    db = get_db()
    settings = get_settings()

    try:
        per_page = int(settings.get("per_page", 50))
    except (ValueError, TypeError):
        per_page = 50
    if per_page not in (10, 20, 30, 50):
        per_page = 50

    page = max(1, request.args.get("page", 1, type=int))
    tag_filter = request.args.get("tag", "", type=str).strip()
    marked_only = request.args.get("marked", "", type=str) == "1"
    search = request.args.get("q", "", type=str).strip()

    where, params, join = [], [], ""
    if tag_filter:
        join = "JOIN photo_tags pt ON p.id = pt.photo_id JOIN tags t ON pt.tag_id = t.id"
        where.append("t.name = ?")
        params.append(tag_filter)
    if marked_only:
        where.append("p.marked_for_removal = 1")
    if search:
        where.append("p.description LIKE ?")
        params.append(f"%{search}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(
        f"SELECT COUNT(DISTINCT p.id) FROM photos p {join} {where_sql}", params
    ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows = db.execute(
        f"""SELECT DISTINCT p.* FROM photos p {join} {where_sql}
            ORDER BY (p.date_taken IS NULL), p.date_taken DESC, p.id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    photo_ids = [r["id"] for r in rows]
    tags_by_photo = {pid: [] for pid in photo_ids}
    if photo_ids:
        qmarks = ",".join("?" * len(photo_ids))
        for photo_id, tag_id, name in db.execute(
            f"""SELECT pt.photo_id, t.id, t.name FROM photo_tags pt
                JOIN tags t ON pt.tag_id = t.id
                WHERE pt.photo_id IN ({qmarks}) ORDER BY t.name""",
            photo_ids,
        ):
            tags_by_photo[photo_id].append({"id": tag_id, "name": name})

    show_zero_tags = settings.get("show_zero_tags") == "1"
    all_tags_count = settings.get("all_tags_count") != "0"
    count_style = settings.get("count_style", "column")

    if show_zero_tags:
        tag_rows = db.execute(
            """SELECT t.name, COUNT(p.id) as count
               FROM tags t
               LEFT JOIN photo_tags pt ON t.id = pt.tag_id
               LEFT JOIN photos p ON pt.photo_id = p.id AND p.marked_for_removal = 0
               GROUP BY t.id, t.name
               ORDER BY t.name"""
        ).fetchall()
    else:
        tag_rows = db.execute(
            """SELECT t.name, COUNT(p.id) as count
               FROM tags t
               JOIN photo_tags pt ON t.id = pt.tag_id
               JOIN photos p ON pt.photo_id = p.id AND p.marked_for_removal = 0
               GROUP BY t.id, t.name
               ORDER BY t.name"""
        ).fetchall()

    total_photos = db.execute("SELECT COUNT(*) FROM photos WHERE marked_for_removal = 0").fetchone()[0]

    max_name_len = max([len(r["name"]) for r in tag_rows] + [len("All tags")], default=8)
    max_cnt_len = max([len(str(r["count"])) for r in tag_rows] + [len(str(total_photos))], default=1)
    col_width = max(34, max_name_len + max_cnt_len + 3)

    if all_tags_count:
        if count_style == "column":
            all_spaces = "\u00A0" * max(2, col_width - len("All tags") - len(str(total_photos)))
            all_tags_display = f"All tags{all_spaces}{total_photos}"
        else:
            all_tags_display = f"All tags ({total_photos})"
    else:
        all_tags_display = "All tags"

    all_tags = []
    for r in tag_rows:
        name = r["name"]
        cnt_str = str(r["count"])
        if count_style == "column":
            spaces = "\u00A0" * max(2, col_width - len(name) - len(cnt_str))
            display = f"{name}{spaces}{cnt_str}"
        else:
            display = f"{name} ({cnt_str})"
        all_tags.append({
            "name": name,
            "count": r["count"],
            "display": display,
        })

    count_4boys = db.execute(
        """SELECT COUNT(DISTINCT pt.photo_id) FROM photo_tags pt
           JOIN tags t ON pt.tag_id = t.id
           JOIN photos p ON pt.photo_id = p.id
           WHERE t.name = '4boys' AND p.marked_for_removal = 0"""
    ).fetchone()[0]

    count_marked = db.execute(
        """SELECT COUNT(*) FROM photos WHERE marked_for_removal = 1
           OR id IN (SELECT photo_id FROM photo_tags pt JOIN tags t ON pt.tag_id = t.id WHERE t.name = 'marked for removal')"""
    ).fetchone()[0]

    return render_template(
        "index.html", photos=rows, tags_by_photo=tags_by_photo, all_tags=all_tags,
        all_tags_display=all_tags_display,
        page=page, total_pages=total_pages, total=total,
        tag_filter=tag_filter, marked_only=marked_only, search=search,
        count_4boys=count_4boys, count_marked=count_marked,
        settings=settings,
    )


@app.route("/image/<int:photo_id>")
def image(photo_id):
    row = get_db().execute("SELECT filepath FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        abort(404)
    path = Path(row["filepath"])
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/thumb/<int:photo_id>")
def thumb(photo_id):
    row = get_db().execute("SELECT filepath FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        abort(404)

    THUMB_DIR.mkdir(exist_ok=True)
    thumb_path = THUMB_DIR / f"{photo_id}.jpg"
    if not thumb_path.exists():
        source = Path(row["filepath"])
        if not source.is_file():
            return send_file(_placeholder_path())
        try:
            img = Image.open(source)
            img.thumbnail(THUMB_SIZE)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(thumb_path, "JPEG", quality=82)
        except Exception:
            return send_file(_placeholder_path())
    return send_file(thumb_path)


_placeholder_cache = None

def _placeholder_path():
    global _placeholder_cache
    if _placeholder_cache is None:
        THUMB_DIR.mkdir(exist_ok=True)
        p = THUMB_DIR / "_unavailable.jpg"
        img = Image.new("RGB", THUMB_SIZE, (60, 60, 60))
        ImageDraw.Draw(img).multiline_text(
            (20, THUMB_SIZE[1] // 2 - 12), "file not found\n(drive connected?)", fill=(230, 230, 230)
        )
        img.save(p, "JPEG")
        _placeholder_cache = p
    return _placeholder_cache


# --------------------------------------------------------------------------
# Actions (AJAX, JSON in/out) -- none of these ever touch the filesystem to delete anything
# --------------------------------------------------------------------------

@app.route("/api/photo/<int:photo_id>/toggle-removal", methods=["POST"])
def toggle_removal(photo_id):
    db = get_db()
    row = db.execute("SELECT marked_for_removal FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if row is None:
        abort(404)
    new_value = 0 if row["marked_for_removal"] else 1
    db.execute("UPDATE photos SET marked_for_removal = ? WHERE id = ?", (new_value, photo_id))
    db.commit()
    return jsonify({"marked_for_removal": bool(new_value)})


@app.route("/api/photo/<int:photo_id>/description", methods=["POST"])
def update_description(photo_id):
    db = get_db()
    if db.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone() is None:
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    description = str(data.get("description", "")).strip()
    db.execute("UPDATE photos SET description = ? WHERE id = ?", (description, photo_id))
    db.commit()
    return jsonify({"description": description})


@app.route("/api/photo/<int:photo_id>/tags", methods=["POST"])
def add_tag(photo_id):
    db = get_db()
    if db.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone() is None:
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip().lower()
    if not name:
        return jsonify({"error": "empty tag"}), 400

    db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag_id = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
    db.execute("INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?)", (photo_id, tag_id))
    db.commit()
    return jsonify({"id": tag_id, "name": name})


@app.route("/api/photo/<int:photo_id>/tags/<int:tag_id>", methods=["DELETE"])
def remove_tag(photo_id, tag_id):
    db = get_db()
    db.execute("DELETE FROM photo_tags WHERE photo_id = ? AND tag_id = ?", (photo_id, tag_id))
    db.commit()
    return jsonify({"removed": True})


# --------------------------------------------------------------------------
# Settings Routes
# --------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    message = None
    msg_type = "success"
    if request.method == "POST":
        new_settings = {
            "all_tags_count": "1" if request.form.get("all_tags_count") == "1" else "0",
            "show_zero_tags": "1" if request.form.get("show_zero_tags") == "1" else "0",
            "count_style": "parens" if request.form.get("count_style") == "parens" else "column",
            "dark_mode": "1" if request.form.get("dark_mode") == "1" else "0",
            "per_page": request.form.get("per_page", "50"),
        }
        if new_settings["per_page"] not in ("10", "20", "30", "50"):
            new_settings["per_page"] = "50"
        save_settings(new_settings)
        message = "Settings saved successfully."

    settings = get_settings()
    current_db = Path(DB_PATH).name
    other_dbs = sorted([f.name for f in Path(".").glob("*.db") if f.name != current_db])
    return render_template(
        "settings.html",
        settings=settings,
        other_dbs=other_dbs,
        current_db=current_db,
        message=message,
        msg_type=msg_type,
    )


@app.route("/settings/export")
def settings_export():
    settings = get_settings()
    response = jsonify(settings)
    response.headers["Content-Disposition"] = 'attachment; filename="photo_catalog_settings.json"'
    return response


@app.route("/settings/import-json", methods=["POST"])
def settings_import_json():
    file = request.files.get("settings_file")
    current_db = Path(DB_PATH).name
    other_dbs = sorted([f.name for f in Path(".").glob("*.db") if f.name != current_db])
    if not file or not file.filename:
        return redirect(url_for("settings_page"))
    try:
        data = json.load(file)
        if isinstance(data, dict):
            save_settings(data)
            return render_template(
                "settings.html",
                settings=get_settings(),
                other_dbs=other_dbs,
                current_db=current_db,
                message="Successfully imported settings from JSON file.",
                msg_type="success",
            )
        else:
            raise ValueError("Invalid format: expected JSON object")
    except Exception as e:
        return render_template(
            "settings.html",
            settings=get_settings(),
            other_dbs=other_dbs,
            current_db=current_db,
            message=f"Failed to import settings: {e}",
            msg_type="error",
        )


@app.route("/settings/import-db", methods=["POST"])
def settings_import_db():
    source_db_name = request.form.get("source_db", "").strip()
    source_path = Path(source_db_name)
    current_db = Path(DB_PATH).name
    other_dbs = sorted([f.name for f in Path(".").glob("*.db") if f.name != current_db])

    if not source_path.is_file() or source_db_name == current_db or source_path.suffix != ".db":
        return render_template(
            "settings.html",
            settings=get_settings(),
            other_dbs=other_dbs,
            current_db=current_db,
            message="Invalid source database selected.",
            msg_type="error",
        )

    try:
        conn = sqlite3.connect(source_path)
        conn.row_factory = sqlite3.Row
        has_table = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()[0]
        if not has_table:
            conn.close()
            return render_template(
                "settings.html",
                settings=get_settings(),
                other_dbs=other_dbs,
                current_db=current_db,
                message=f"No settings table found in '{source_db_name}'.",
                msg_type="error",
            )
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        imported = {row["key"]: row["value"] for row in rows}
        conn.close()
        if imported:
            save_settings(imported)
            msg = f"Successfully copied {len(imported)} settings from '{source_db_name}'."
            m_type = "success"
        else:
            msg = f"No settings found in '{source_db_name}'."
            m_type = "error"
    except Exception as e:
        return render_template(
            "settings.html",
            settings=get_settings(),
            other_dbs=other_dbs,
            current_db=current_db,
            message=f"Error reading '{source_db_name}': {e}",
            msg_type="error",
        )

    return render_template(
        "settings.html",
        settings=get_settings(),
        other_dbs=other_dbs,
        current_db=current_db,
        message=msg,
        msg_type=m_type,
    )


# --------------------------------------------------------------------------

def main():
    global DB_PATH
    parser = argparse.ArgumentParser(description="Local web viewer/editor for photos.db")
    parser.add_argument("--db", default="photos.db", help="Path to photos.db (default: photos.db)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0, reachable on your LAN)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()

    if not Path(args.db).is_file():
        raise SystemExit(f"Database not found: {args.db}")

    DB_PATH = args.db
    migrate(DB_PATH)
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
