# Release Notes

## Release - October 10, 2026: Phase 1 - Authentication, Authorization, Per-User Settings & Activity Auditing

### Overview
This release implements Phase 1 of the identity and security architecture. It adds mandatory authentication across all catalog pages, media routes, and API endpoints, introduces CLI tools for bootstrap administration, per-user configuration isolation, a full activity audit log for tracking catalog changes, and an admin-only user management interface.

---

### New Features & Enhancements

#### 1. Mandatory Identity & Authentication
- **Universal Route Guard**: All web routes, photo/thumbnail serving endpoints, and REST API endpoints now enforce authentication via a `before_request` hook.
- **Login Flow**: Unauthenticated web visitors are redirected to `/login` with safe `next` parameter redirection. Unauthenticated API calls return HTTP `401 Unauthorized`.
- **Session Security**: Session tokens are signed using persistent secret keys stored in `.secret_key` (excluded from git).
- **Password Security**: Passwords are cryptographically salted and hashed using Werkzeug (`scrypt`/`pbkdf2:sha256`).
- **User Navigation**: All pages now display a header user pill showing the active username, an `admin` role badge (if applicable), links to the Settings and Audit Log, and a Logout button.

#### 2. Command-Line Administration (CLI)
- **Bootstrap Admin Creation**: Provision the initial administrator account directly from the command line:
  ```bash
  python3 app.py --db photos.db --create-admin <username>
  ```
- **CLI User Management Subcommands**:
  - `--create-user <username> [--role user|admin]`
  - `--reset-password <username>`
  - `--list-users`
- Interactive password prompts with confirmation when `--password` is omitted.

#### 3. Administrative User Management Interface
- **Admin Panel in Settings**: Administrators have access to an integrated user management section on the `/settings` page.
- **Account Creation**: Create new users and designate roles (`user` or `admin`) directly from the web interface.
- **User Directory**: View all registered users, roles, account creation dates, and last login timestamps.
- **Account Deletion**: Administrators can remove users with built-in safeguards preventing self-deletion.
- **Access Control**: Non-administrative users attempting to access user creation or deletion endpoints are rejected with HTTP `403 Forbidden`.

#### 4. Per-User Configuration Settings
- **Isolated User Settings**: Configuration options (tag count display, straight column alignment, zero-count tag hiding, per-page photo count, and Dark Mode) are now saved independently per user in the `user_settings` table.
- **Global Fallback**: If a user hasn't explicitly customized a setting, the application smoothly falls back to the server's global defaults.
- **Personal Dark Mode**: Dark Mode preference is bound to individual user accounts rather than server-wide.

#### 5. Activity Auditing & Change History (`/audit`)
- **Mutation Tracking**: Every catalog modification records an immutable log entry in `audit_log`:
  - Marking / unmarking photos for removal
  - Adding or removing photo tags
  - Editing photo descriptions
  - User creation and deletion actions
- **Audit Detail**: Records store the timestamp, user ID, username, action type, affected photo ID, filename, and JSON metadata detailing what changed.
- **Dedicated Audit Viewer**: A responsive `/audit` page accessible to all authenticated users:
  - Filter history by user or by action type.
  - Color-coded badges for different action categories (`mark_removal`, `add_tag`, `edit_description`, etc.).
  - Chronological pagination with direct photo links.
- **Shared Catalog Integrity**: Catalog updates made by any user remain shared and immediately visible to all users.

---

### Files Modified & Created

- **`app.py`**:
  - Added database migrations for `users`, `user_settings`, `audit_log`, and `tag_permissions` tables.
  - Implemented persistent Flask `secret_key` loader.
  - Added CLI flags and handler functions (`cli_create_admin`, `cli_create_user`, `cli_reset_password`, `cli_list_users`).
  - Added `before_request` auth hook, template context processor, `/login`, `/logout`, `/audit`, and `/admin/users/*` routes.
  - Implemented `get_user_settings()` and `save_user_settings()` with fallback to global settings.
  - Added `record_audit()` logging calls to photo mutation API endpoints.
- **`AUTH_AND_AUDIT_PLAN.md`** *(New)*:
  - Complete architectural blueprint covering threat model, schema migrations, phased roadmap, and verification steps.
- **`templates/login.html`** *(New)*:
  - Clean login form supporting return redirect and theme integration.
- **`templates/audit.html`** *(New)*:
  - Filterable activity audit history table with pagination and action badges.
- **`templates/index.html`**:
  - Added user pill, Audit Log navigation link, and Logout button to header.
- **`templates/settings.html`**:
  - Added user pill, Audit Log link, and Admin User Management card.
- **`static/style.css`**:
  - Added styling for login page, user status pill, audit table, action badges, admin user creation form, and dark mode variants.
- **`.gitignore`**:
  - Excluded `.secret_key`, `__pycache__/`, `*.pyc`, and database backup files.
- **`RELEASE_NOTES.md`**:
  - Updated documentation with October 10, 2026 Phase 1 release details.

---

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
