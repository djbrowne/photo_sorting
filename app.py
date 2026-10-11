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
import getpass
import json
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_file, abort, g, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageDraw

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

app = Flask(__name__)

SECRET_KEY_FILE = Path(".secret_key")

def get_or_create_secret_key():
    if SECRET_KEY_FILE.is_file():
        try:
            k = SECRET_KEY_FILE.read_text().strip()
            if k:
                return k
        except Exception:
            pass
    k = secrets.token_hex(32)
    try:
        SECRET_KEY_FILE.write_text(k)
    except Exception:
        pass
    return k

app.secret_key = get_or_create_secret_key()

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


DB_TIMEOUT = 15.0  # seconds to wait for database locks before raising OperationalError


# --------------------------------------------------------------------------
# Database & Settings
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
        g.db.row_factory = sqlite3.Row
        g.db.execute(f"PRAGMA busy_timeout = {int(DB_TIMEOUT * 1000)}")
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_write_with_retry(fn, max_retries=3, delay=0.25):
    """Execute a database write function, retrying if the database is locked."""
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if ("locked" in str(e).lower() or "busy" in str(e).lower()) and attempt < max_retries - 1:
                time.sleep(delay * (2 ** attempt))
                continue
            raise


def get_user_settings(user_id=None):
    try:
        db = get_db()
        s = dict(DEFAULT_SETTINGS)
        for row in db.execute("SELECT key, value FROM settings"):
            s[row["key"]] = row["value"]
        if user_id:
            for row in db.execute("SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)):
                s[row["key"]] = row["value"]
        return s
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return dict(DEFAULT_SETTINGS)
        raise


def get_settings(user_id=None):
    if user_id is None and hasattr(g, "current_user") and g.current_user:
        user_id = g.current_user["id"]
    return get_user_settings(user_id)


def save_user_settings(user_id, new_settings):
    def _save():
        db = get_db()
        for k in DEFAULT_SETTINGS:
            if k in new_settings:
                db.execute(
                    "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                    (user_id, k, str(new_settings[k])),
                )
        db.commit()
    db_write_with_retry(_save)


def save_settings(new_settings):
    def _save():
        db = get_db()
        for k in DEFAULT_SETTINGS:
            if k in new_settings:
                db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(new_settings[k])))
        db.commit()
    db_write_with_retry(_save)


def migrate(db_path):
    """Ensure database schema is up-to-date with auth, settings, and audit tables."""
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.execute("PRAGMA busy_timeout = 15000")
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            access_level INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )"""
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
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_photo ON audit_log(photo_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tag_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_name TEXT UNIQUE NOT NULL,
            min_access_level INTEGER NOT NULL DEFAULT 1,
            restricted_to_role TEXT
        )"""
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Authentication & Audit Logging
# --------------------------------------------------------------------------

def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        db = get_db()
        row = db.execute(
            "SELECT id, username, role, access_level, created_at, last_login FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            session.pop("user_id", None)
            return None
        return dict(row)
    except sqlite3.OperationalError as e:
        if ("locked" in str(e).lower() or "busy" in str(e).lower()) and session.get("username"):
            return {
                "id": user_id,
                "username": session.get("username", "user"),
                "role": session.get("role", "user"),
                "access_level": session.get("access_level", 1),
            }
        raise


@app.before_request
def require_login():
    g.current_user = get_current_user()
    if request.endpoint in ("login", "logout", "static") or request.path.startswith("/static/"):
        return
    if not g.current_user:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        return redirect(url_for("login", next=request.full_path if request.method == "GET" else url_for("index")))


@app.context_processor
def inject_globals():
    user = getattr(g, "current_user", None)
    try:
        settings = get_settings(user["id"] if user else None)
    except Exception:
        settings = DEFAULT_SETTINGS
    return {
        "current_user": user,
        "current_settings": settings,
    }


def record_audit(action, photo_id=None, details=None):
    def _record():
        user = getattr(g, "current_user", None)
        user_id = user["id"] if user else None
        username = user["username"] if user else "system"
        db = get_db()
        filename = None
        if photo_id:
            row = db.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
            if row:
                filename = row["filename"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        details_str = details if isinstance(details, str) else json.dumps(details) if details else None
        db.execute(
            """INSERT INTO audit_log (timestamp, user_id, username, action, photo_id, photo_filename, details)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, user_id, username, action, photo_id, filename, details_str),
        )
        db.commit()

    try:
        db_write_with_retry(_record, max_retries=3, delay=0.2)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            print(f"Warning: could not record audit log entry ({action}) due to database lock: {e}", file=sys.stderr)
        else:
            raise


@app.errorhandler(sqlite3.OperationalError)
def handle_db_operational_error(e):
    error_msg = str(e).lower()
    is_locked = "locked" in error_msg or "busy" in error_msg

    # Determine if API / AJAX or browser page request
    is_api = (
        request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )

    if is_api:
        msg = (
            "The database is temporarily locked by another process. Please wait a moment and try again."
            if is_locked
            else f"Database error: {e}"
        )
        return jsonify({"error": msg, "locked": is_locked}), 503

    current_settings = DEFAULT_SETTINGS
    try:
        user = getattr(g, "current_user", None)
        if user:
            current_settings = get_settings(user["id"])
    except Exception:
        pass

    db_name = Path(DB_PATH).name
    try:
        return (
            render_template(
                "db_locked.html",
                db_name=db_name,
                is_locked=is_locked,
                error_detail=str(e),
                current_settings=current_settings,
                settings=current_settings,
            ),
            503,
        )
    except Exception:
        return (
            f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="5">
<title>Database Temporarily Busy</title>
<style>body{{font-family:sans-serif;text-align:center;padding:50px;background:#f8fafc;color:#1e293b;}}
.card{{max-width:500px;margin:auto;padding:30px;background:white;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,0.1);}}
button{{padding:10px 20px;font-size:16px;background:#2b6cb0;color:white;border:none;border-radius:6px;cursor:pointer;margin-top:15px;}}</style>
</head><body><div class="card">
<div style="font-size:40px;margin-bottom:10px;">⏳</div>
<h2>Database Temporarily Busy</h2>
<p>The database (<code>{db_name}</code>) is currently locked by another operation. Retrying automatically in 5 seconds...</p>
<button onclick="window.location.reload()">Retry Now</button>
</div></body></html>""",
            503,
        )


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
    def _toggle():
        db = get_db()
        row = db.execute("SELECT marked_for_removal FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if row is None:
            abort(404)
        new_value = 0 if row["marked_for_removal"] else 1
        db.execute("UPDATE photos SET marked_for_removal = ? WHERE id = ?", (new_value, photo_id))
        db.commit()
        return new_value

    new_value = db_write_with_retry(_toggle)
    record_audit(
        "mark_removal" if new_value else "unmark_removal",
        photo_id=photo_id,
        details={"marked_for_removal": bool(new_value)},
    )
    return jsonify({"marked_for_removal": bool(new_value)})


@app.route("/api/photo/<int:photo_id>/description", methods=["POST"])
def update_description(photo_id):
    data = request.get_json(force=True, silent=True) or {}
    description = str(data.get("description", "")).strip()

    def _update():
        db = get_db()
        old_row = db.execute("SELECT id, description FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if old_row is None:
            abort(404)
        db.execute("UPDATE photos SET description = ? WHERE id = ?", (description, photo_id))
        db.commit()
        return old_row["description"] or ""

    prev_desc = db_write_with_retry(_update)
    record_audit(
        "edit_description",
        photo_id=photo_id,
        details={"previous": prev_desc, "new": description},
    )
    return jsonify({"description": description})


@app.route("/api/photo/<int:photo_id>/tags", methods=["POST"])
def add_tag(photo_id):
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip().lower()
    if not name:
        return jsonify({"error": "empty tag"}), 400

    def _add():
        db = get_db()
        if db.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone() is None:
            abort(404)
        db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?)", (photo_id, tag_id))
        db.commit()
        return tag_id

    tag_id = db_write_with_retry(_add)
    record_audit("add_tag", photo_id=photo_id, details={"tag": name})
    return jsonify({"id": tag_id, "name": name})


@app.route("/api/photo/<int:photo_id>/tags/<int:tag_id>", methods=["DELETE"])
def remove_tag(photo_id, tag_id):
    db = get_db()
    tag_row = db.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
    tag_name = tag_row["name"] if tag_row else str(tag_id)

    def _remove():
        db = get_db()
        db.execute("DELETE FROM photo_tags WHERE photo_id = ? AND tag_id = ?", (photo_id, tag_id))
        db.commit()

    db_write_with_retry(_remove)
    record_audit("remove_tag", photo_id=photo_id, details={"tag": tag_name})
    return jsonify({"removed": True})


# --------------------------------------------------------------------------
# Authentication Routes
# --------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user:
        return redirect(url_for("index"))
    error = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("index")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            try:
                db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user["id"]))
                db.commit()
            except sqlite3.OperationalError:
                pass
            return redirect(next_url)
        else:
            error = "Invalid username or password."

    return render_template("login.html", error=error, next=next_url)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Audit Routes
# --------------------------------------------------------------------------

@app.route("/audit")
def audit_view():
    db = get_db()
    page = max(1, request.args.get("page", 1, type=int))
    user_filter = request.args.get("user", "").strip()
    action_filter = request.args.get("action", "").strip()
    per_page = 50

    where, params = [], []
    if user_filter:
        where.append("username = ?")
        params.append(user_filter)
    if action_filter:
        where.append("action = ?")
        params.append(action_filter)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.execute(f"SELECT COUNT(*) FROM audit_log {where_sql}", params).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows = db.execute(
        f"""SELECT id, timestamp, user_id, username, action, photo_id, photo_filename, details
            FROM audit_log {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?""",
        params + [per_page, offset],
    ).fetchall()

    all_users = [r[0] for r in db.execute("SELECT DISTINCT username FROM audit_log ORDER BY username").fetchall()]
    all_actions = [r[0] for r in db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()]

    return render_template(
        "audit.html",
        logs=rows,
        page=page,
        total_pages=total_pages,
        total=total,
        user_filter=user_filter,
        action_filter=action_filter,
        all_users=all_users,
        all_actions=all_actions,
    )


# --------------------------------------------------------------------------
# Admin User Management Routes
# --------------------------------------------------------------------------

@app.route("/admin/users/create", methods=["POST"])
def admin_create_user():
    if not g.current_user or g.current_user["role"] != "admin":
        abort(403)
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user").strip()
    if role not in ("admin", "user"):
        role = "user"

    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect(url_for("settings_page"))

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        flash(f"User '{username}' already exists.", "error")
        return redirect(url_for("settings_page"))

    p_hash = generate_password_hash(password)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _create():
        db = get_db()
        db.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, p_hash, role, now),
        )
        db.commit()

    db_write_with_retry(_create)
    record_audit("create_user", details={"created_username": username, "role": role})
    flash(f"User '{username}' ({role}) created successfully.", "success")
    return redirect(url_for("settings_page"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    if not g.current_user or g.current_user["role"] != "admin":
        abort(403)
    if user_id == g.current_user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("settings_page"))

    db = get_db()
    user = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        def _delete():
            db = get_db()
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
            db.commit()

        db_write_with_retry(_delete)
        record_audit("delete_user", details={"deleted_username": user["username"]})
        flash(f"User '{user['username']}' deleted.", "success")
    return redirect(url_for("settings_page"))


# --------------------------------------------------------------------------
# Settings Routes
# --------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    message = None
    msg_type = "success"
    user_id = g.current_user["id"] if g.current_user else None

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
        if user_id:
            save_user_settings(user_id, new_settings)
        else:
            save_settings(new_settings)
        message = "Settings saved successfully."

    settings = get_settings(user_id)
    db = get_db()
    current_db = Path(DB_PATH).name
    other_dbs = sorted([f.name for f in Path(".").glob("*.db") if f.name != current_db])

    user_list = []
    if g.current_user and g.current_user["role"] == "admin":
        user_list = db.execute(
            "SELECT id, username, role, access_level, created_at, last_login FROM users ORDER BY username"
        ).fetchall()

    return render_template(
        "settings.html",
        settings=settings,
        other_dbs=other_dbs,
        current_db=current_db,
        message=message,
        msg_type=msg_type,
        user_list=user_list,
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
    user_id = g.current_user["id"] if g.current_user else None
    current_db = Path(DB_PATH).name
    other_dbs = sorted([f.name for f in Path(".").glob("*.db") if f.name != current_db])
    if not file or not file.filename:
        return redirect(url_for("settings_page"))
    try:
        data = json.load(file)
        if isinstance(data, dict):
            if user_id:
                save_user_settings(user_id, data)
            else:
                save_settings(data)
            return render_template(
                "settings.html",
                settings=get_settings(user_id),
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
            settings=get_settings(user_id),
            other_dbs=other_dbs,
            current_db=current_db,
            message=f"Failed to import settings: {e}",
            msg_type="error",
        )


@app.route("/settings/import-db", methods=["POST"])
def settings_import_db():
    source_db_name = request.form.get("source_db", "").strip()
    source_path = Path(source_db_name)
    user_id = g.current_user["id"] if g.current_user else None
    current_db = Path(DB_PATH).name
    other_dbs = sorted([f.name for f in Path(".").glob("*.db") if f.name != current_db])

    if not source_path.is_file() or source_db_name == current_db or source_path.suffix != ".db":
        return render_template(
            "settings.html",
            settings=get_settings(user_id),
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
                settings=get_settings(user_id),
                other_dbs=other_dbs,
                current_db=current_db,
                message=f"No settings table found in '{source_db_name}'.",
                msg_type="error",
            )
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        imported = {row["key"]: row["value"] for row in rows}
        conn.close()
        if imported:
            if user_id:
                save_user_settings(user_id, imported)
            else:
                save_settings(imported)
            msg = f"Successfully copied {len(imported)} settings from '{source_db_name}'."
            m_type = "success"
        else:
            msg = f"No settings found in '{source_db_name}'."
            m_type = "error"
    except Exception as e:
        return render_template(
            "settings.html",
            settings=get_settings(user_id),
            other_dbs=other_dbs,
            current_db=current_db,
            message=f"Error reading '{source_db_name}': {e}",
            msg_type="error",
        )

    return render_template(
        "settings.html",
        settings=get_settings(user_id),
        other_dbs=other_dbs,
        current_db=current_db,
        message=msg,
        msg_type=m_type,
    )


# --------------------------------------------------------------------------
# CLI Management Commands
# --------------------------------------------------------------------------

def cli_create_admin(db_path, username, password=None):
    migrate(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        print(f"Error: User '{username}' already exists.")
        conn.close()
        return 1
    if not password:
        password = getpass.getpass(f"Enter password for admin '{username}': ")
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Error: Passwords do not match.")
            conn.close()
            return 1
    if not password.strip():
        print("Error: Password cannot be empty.")
        conn.close()
        return 1
    p_hash = generate_password_hash(password)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO users (username, password_hash, role, access_level, created_at) VALUES (?, ?, 'admin', 99, ?)",
        (username, p_hash, now),
    )
    conn.commit()
    conn.close()
    print(f"Admin user '{username}' created successfully!")
    return 0


def cli_create_user(db_path, username, role="user", password=None):
    migrate(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        print(f"Error: User '{username}' already exists.")
        conn.close()
        return 1
    if not password:
        password = getpass.getpass(f"Enter password for user '{username}': ")
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Error: Passwords do not match.")
            conn.close()
            return 1
    if not password.strip():
        print("Error: Password cannot be empty.")
        conn.close()
        return 1
    p_hash = generate_password_hash(password)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO users (username, password_hash, role, access_level, created_at) VALUES (?, ?, ?, 1, ?)",
        (username, p_hash, role, now),
    )
    conn.commit()
    conn.close()
    print(f"User '{username}' ({role}) created successfully!")
    return 0


def cli_reset_password(db_path, username, password=None):
    migrate(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not existing:
        print(f"Error: User '{username}' not found.")
        conn.close()
        return 1
    if not password:
        password = getpass.getpass(f"Enter new password for '{username}': ")
        password_confirm = getpass.getpass("Confirm new password: ")
        if password != password_confirm:
            print("Error: Passwords do not match.")
            conn.close()
            return 1
    if not password.strip():
        print("Error: Password cannot be empty.")
        conn.close()
        return 1
    p_hash = generate_password_hash(password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (p_hash, existing["id"]))
    conn.commit()
    conn.close()
    print(f"Password for user '{username}' updated successfully!")
    return 0


def cli_list_users(db_path):
    migrate(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, username, role, access_level, created_at, last_login FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        print("No users found in database.")
        return 0
    print(f"{'ID':<4} {'Username':<18} {'Role':<8} {'Level':<6} {'Created At':<20} {'Last Login':<20}")
    print("-" * 78)
    for r in rows:
        created = r["created_at"] or ""
        last = r["last_login"] or "Never"
        print(f"{r['id']:<4} {r['username']:<18} {r['role']:<8} {r['access_level']:<6} {created:<20} {last:<20}")
    return 0


# --------------------------------------------------------------------------

def main():
    global DB_PATH
    parser = argparse.ArgumentParser(description="Local web viewer/editor for photos.db")
    parser.add_argument("--db", default="photos.db", help="Path to photos.db (default: photos.db)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0, reachable on your LAN)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")

    # User management CLI options
    parser.add_argument("--create-admin", metavar="USERNAME", help="Create an admin user via CLI")
    parser.add_argument("--create-user", metavar="USERNAME", help="Create a standard user via CLI")
    parser.add_argument("--role", default="user", choices=["user", "admin"], help="Role for --create-user (default: user)")
    parser.add_argument("--password", metavar="PASSWORD", help="Password for user creation/reset (optional; if omitted, prompts securely)")
    parser.add_argument("--reset-password", metavar="USERNAME", help="Reset password for an existing user")
    parser.add_argument("--list-users", action="store_true", help="List all registered users")

    args = parser.parse_args()

    if not Path(args.db).is_file():
        raise SystemExit(f"Database not found: {args.db}")

    DB_PATH = args.db

    # Handle CLI subcommands if specified
    if args.create_admin:
        sys.exit(cli_create_admin(DB_PATH, args.create_admin, args.password))
    if args.create_user:
        sys.exit(cli_create_user(DB_PATH, args.create_user, args.role, args.password))
    if args.reset_password:
        sys.exit(cli_reset_password(DB_PATH, args.reset_password, args.password))
    if args.list_users:
        sys.exit(cli_list_users(DB_PATH))

    migrate(DB_PATH)
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
