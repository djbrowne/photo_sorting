# Release Notes

## Release - October 9, 2026

### Overview
This release introduces user-configurable settings, full Dark Mode support, tag count metrics in the gallery filter dropdown, active photo count filtering, dynamic pagination, and cross-database configuration transfer tools.

---

### New Features & Enhancements

#### 1. Tag Dropdown Counts & Straight Right Margin
- **Photo Counts in Dropdown**: Each tag in the filter dropdown displays the total number of images associated with that tag in the active database.
- **Total Count on "All tags"**: The top option displays the total count of active photos cataloged in the database.
- **Right-Justified Column Alignment**: Options use non-breaking space padding (`\u00A0`) and monospace font formatting (`ui-monospace`, `Consolas`, `monospace`) to produce a straight vertical right margin for all counts.
- **Active Photos Only**: Photos flagged as `marked for removal` are automatically excluded from tag and total counts, reflecting only active images in the catalog.
- **Zero-Count Tag Filtering**: Tags with zero active photos are hidden from the dropdown by default.

#### 2. Main Page Header Badges & Live Updates
- **Global Stat Badges**: Added top-right header labels displaying live counts for `4boys` and `marked for removal` photos across the database.
- **Filter Links**: Clicking the badges immediately filters the photo gallery.
- **Real-Time Client Updates**: Checking/unchecking the "mark for removal" box or adding/removing the `4boys` tag updates badge counts in real time via JavaScript without requiring a page reload.

#### 3. Server-Side Settings Architecture
- **Database-Backed Configuration**: Settings are stored in a dedicated `settings` table (`key TEXT PRIMARY KEY, value TEXT NOT NULL`) directly within the active SQLite database.
- **Server-Wide Persistence**: Preferences apply globally to all users accessing the catalog on the local network.
- **Automatic Schema Migration**: The `settings` table is created automatically on startup or initial database access if missing.

#### 4. Dedicated Settings Page (`/settings`)
- **Navigation**: Accessible from the main gallery header via a gear icon (`⚙`).
- **Tag Dropdown Display Options**:
  - Toggle whether the total photo count is shown on the "All tags" option.
  - Toggle whether zero-count tags are shown or hidden.
  - Select count display style:
    - **Column padding**: Monospace font with straight right-justified counts.
    - **Parentheses**: Standard font formatting `tag (count)`.
- **Configurable Pagination**:
  - Choose images displayed per page: `10`, `20`, `30`, or `50` photos.
- **Appearance (Dark Mode)**:
  - Global Dark Mode toggle supporting both the main catalog and the settings page.

#### 5. Dark Mode Theme
- **Complete Dark Theme**: Implemented a modern dark theme (`body.dark`) using zinc/slate tones (`#18181b` background, `#27272a` cards and inputs, `#3f3f46` borders, `#e4e4e7` text).
- **Styled Elements**: Dark mode styling covers headers, thumbnail containers, filter controls, textareas, quick-tag buttons, status badges, pagination, and settings cards.

#### 6. Database Configuration Transfer & Backup
- **Import from Another Database**: Select any other `.db` file in the application directory and copy its configuration table directly into the active database with one click.
- **Export Settings (JSON)**: Download the current configuration as a `photo_catalog_settings.json` file.
- **Import Settings (JSON)**: Upload a JSON settings file to restore or apply configurations from another machine.

---

### Files Modified & Created

- **`app.py`**:
  - Implemented `get_settings()` and `save_settings()` functions.
  - Added database migration for `settings` table.
  - Updated `index()` route with dynamic pagination, tag counts, zero-count filtering, and count formatting.
  - Added routes: `/settings`, `/settings/export`, `/settings/import-json`, and `/settings/import-db`.
- **`templates/index.html`**:
  - Added gear icon link in header to navigate to Settings.
  - Bound dynamic dark mode class to `<body>`.
  - Added conditional monospace class for tag dropdown.
- **`templates/settings.html`** *(New)*:
  - Form controls for dropdown display, appearance, pagination, and config transfer tools.
- **`static/style.css`**:
  - Added `.settings-link` and `.back-link` styles.
  - Added `.monospace-select` style.
  - Added `.settings-container`, `.settings-card`, button, alert, and transfer card styles.
  - Added full `body.dark` styling palette.
- **`RELEASE_NOTES.md`** *(New)*:
  - Full documentation of changes and features introduced in this release.
