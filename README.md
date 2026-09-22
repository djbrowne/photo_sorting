# Photo Sorting & Catalog

A local, AI-assisted photo cataloging and curation suite designed for large personal photo collections stored on local drives or network shares.

It uses local vision models running in [Ollama](https://ollama.com) (such as `llava` or `moondream`) to automatically generate content descriptions and subject tags, extracts file and EXIF metadata (capture dates, dimensions, GPS coordinates), and provides a clean, responsive web interface for browsing, searching, and organizing photos.

---

## Features

- **Local AI Tagging & Descriptions**:
  - Automatically identifies photo subjects (e.g. `bird`, `landscape`, `car`, `person`, `building`).
  - Generates factual sentence descriptions for each image.
  - Fully local processing via Ollama—no cloud subscriptions, API keys, or private photos sent over the internet.
- **Metadata & EXIF Extraction**:
  - Extracts image dimensions, file size, and capture timestamps (`DateTimeOriginal`).
  - Parses GPS coordinates (latitude and longitude) into decimal degrees.
  - Broad format support including `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.tiff`, `.tif`, and Apple `.heic`/`.heif` files.
- **Resumable Ingestion**:
  - Tracks processing state in SQLite (`status = 'done'` or `'error'`).
  - Safe to interrupt (Ctrl+C) and resume at any time without reprocessing already completed files.
  - Supports `--limit` for quick trial runs on new folders.
- **Fast, Responsive Web Interface (`app.py`)**:
  - Paginated photo gallery (60 photos per page) sorted by date taken.
  - On-demand caching of 320×320 thumbnails in a local `thumbnails/` folder.
  - Graceful placeholder indicator if source drives/network mounts are disconnected.
  - Instant full-resolution photo viewing in a new tab.
  - Filter by tag, search descriptions by text, or filter by flagged removals.
  - **Inline Curation & Tagging**:
    - One-click quick-tag buttons (`+ bird`, `+ 4boys`) for fast subject labeling.
    - Add custom tags or delete existing tags.
    - Edit and save photo descriptions inline.
    - **Safe / Non-Destructive Removal Marking**: Toggle a "mark for removal" flag to identify photos for culling without deleting anything from disk or the database.

---

## Architecture & Project Structure

```text
photo_sorting/
├── catalog_photos.py      # CLI script: scans drives, extracts EXIF, runs Ollama vision models
├── app.py                 # Flask web server and JSON REST API
├── photos.db              # SQLite database (photos, tags, photo_tags)
├── templates/
│   └── index.html         # Jinja2 template for gallery, filters, and photo cards
├── static/
│   ├── app.js             # Client-side AJAX event handling (tags, descriptions, removal flags)
│   └── style.css          # Gallery layout and card styling
├── thumbnails/            # Auto-generated thumbnail cache (ignored by git)
└── README.md
```

---

## Prerequisites

1. **Python 3.8+**
2. **Ollama**: Download and install from [ollama.com](https://ollama.com).
3. **Vision Model**: Pull a vision model in Ollama:
   ```bash
   # Small and fast (good for older or limited GPUs)
   ollama pull moondream

   # Larger, higher accuracy
   ollama pull llava
   ```

---

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/djbrowne/photo_sorting.git
   cd photo_sorting
   ```

2. **Install Python dependencies:**
   ```bash
   pip install flask pillow requests

   # Required if your library contains iPhone HEIC/HEIF photos:
   pip install pillow-heif
   ```

---

## Usage

### 1. Catalog Photos

Run `catalog_photos.py` pointing to one or more photo directories:

```bash
# Test run on the first 50 new photos to check tag quality and speed:
python3 catalog_photos.py /path/to/photos --limit 50

# Full run across one or multiple directories:
python3 catalog_photos.py /mnt/drive1/photos /mnt/drive2/photos

# Specify a custom database, model, or Ollama host:
python3 catalog_photos.py /mnt/drive1/photos \
  --db photos.db \
  --model llava \
  --host http://localhost:11434
```

**Options:**
- `roots`: One or more paths to scan recursively.
- `--db PATH`: Path to SQLite database (default: `photos.db`).
- `--model MODEL`: Ollama vision model (default: `llava` or `moondream`).
- `--host URL`: Ollama API base URL (default: `http://localhost:11434`).
- `--limit N`: Stop after processing `N` new photos.

---

### 2. Launch Web Viewer & Editor

Start the local Flask server:

```bash
python3 app.py --db photos.db --port 8080
```

Open your browser and navigate to:
- Locally: `http://localhost:8080`
- From any device on your local LAN: `http://<your-machine-ip>:8080`

**Options:**
- `--db PATH`: Path to the SQLite database (default: `photos.db`).
- `--host HOST`: Bind host (default: `0.0.0.0` for LAN access).
- `--port PORT`: Port number (default: `8080`).

---

## Database Schema

The database consists of three core tables:

```sql
-- Photos metadata and curation flags
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
    status TEXT DEFAULT 'pending',   -- 'done' or 'error'
    error_message TEXT,
    processed_at TEXT,
    marked_for_removal INTEGER NOT NULL DEFAULT 0
);

-- Distinct tags
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

-- Many-to-many relationship
CREATE TABLE photo_tags (
    photo_id INTEGER NOT NULL REFERENCES photos(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (photo_id, tag_id)
);
```

### Useful SQL Queries

You can inspect `photos.db` with `sqlite3 photos.db`:

```sql
-- Find all photos tagged as 'bird'
SELECT p.filepath FROM photos p
JOIN photo_tags pt ON p.id = pt.photo_id
JOIN tags t ON pt.tag_id = t.id
WHERE t.name = 'bird';

-- Top 20 most frequent tags
SELECT t.name, COUNT(*) AS count FROM tags t
JOIN photo_tags pt ON t.id = pt.tag_id
GROUP BY t.name ORDER BY count DESC LIMIT 20;

-- List photos marked for removal
SELECT id, filename, filepath FROM photos WHERE marked_for_removal = 1;

-- Photos with GPS location data
SELECT filename, gps_lat, gps_lon FROM photos
WHERE gps_lat IS NOT NULL ORDER BY gps_lat, gps_lon;

-- Photos that encountered processing errors
SELECT filepath, error_message FROM photos WHERE status = 'error';
```

---

## License

This project is open-source under the MIT License.
