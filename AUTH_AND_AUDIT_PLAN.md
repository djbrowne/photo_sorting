# Identity, Authentication, Authorization & Auditing Plan

## 1. Executive Summary & Goals

This document specifies the technical design, database schema, and implementation plan for adding identity, authentication, authorization, per-user configuration, and change auditing to the Photo Catalog application.

### Core Objectives:
1. **Mandatory Login**: All pages, static photos, thumbnails, and mutation APIs require authentication.
2. **Per-User Configuration**: Each user has their own preferences (Dark Mode, images per page, tag dropdown display style).
3. **Change Auditing**: An immutable audit log records who made what change and when (marking for removal, adding/removing tags, editing descriptions).
4. **Shared Catalog State**: The state of the catalog (photo removals, tags, descriptions) remains globally shared and real-time across all users.
5. **Future Tag-Based Authorization (Phase 2)**: Architectural hooks and schema foundations to restrict viewing of specific images based on subject tags and user access levels.

---

## 2. Key Architectural Decisions

Based on design alignment:

1. **Initial Admin Provisioning**:
   - **Option C**: The initial admin account will be created via a dedicated CLI command:
     ```bash
     python3 app.py --create-admin <username>
     # Or: python3 app.py --create-user <username> --role admin
     ```
   - Passwords will be prompted securely via `getpass` (without echo) or passed via argument.
2. **User Account Creation Policy**:
   - **Admin-Only Provisioning**: Only authenticated administrators can create, update, or deactivate user accounts via the Settings/Admin panel. No public self-registration.
3. **Audit Log Visibility**:
   - **All Logged-In Users**: All authenticated users can view the audit history log to ensure transparency and collaborative curation.
4. **Phased Rollout**:
   - **Phase 1**: Authentication, CLI Admin creation, Admin User Management, Per-User Settings, and Mutation Auditing.
   - **Phase 2**: Tag-based access controls and granular permission rules.

---

## 3. System Architecture Diagram

```
                                      ┌─────────────────────────────────┐
                                      │        Client / Browser         │
                                      └────────────────┬────────────────┘
                                                       │
                                     Session Cookie (Encrypted/Signed)
                                                       │
                                      ┌────────────────▼────────────────┐
                                      │   Flask before_request Hook     │
                                      │  (Authenticates Session User)   │
                                      └───────┬────────────────┬────────┘
                    Unauthenticated / Missing │                │ Authenticated (g.current_user)
                                              ▼                ▼
                                      ┌───────────────┐  ┌──────────────────────────────────┐
                                      │ /login Page   │  │ Protected Routes:                │
                                      │ (Form & Auth) │  │ - / (Gallery & Filters)          │
                                      └───────────────┘  │ - /image & /thumb (Secure Media) │
                                                         │ - /api/... (Mutation APIs)       │
                                                         │ - /settings (User Settings)      │
                                                         │ - /audit (Activity History)      │
                                                         │ - /admin/users (Admin Only)      │
                                                         └────────────────┬─────────────────┘
                                                                          │
                                               ┌──────────────────────────┴──────────────────────────┐
                                               │                                                     │
                                               ▼                                                     ▼
                                    ┌──────────────────────┐                             ┌──────────────────────┐
                                    │    Audit Logging     │                             │  Authorization Gate  │
                                    │  - user_id, username │                             │ - Shared photo state │
                                    │  - action & details  │                             │ - (Phase 2: Tag-level│
                                    │  - photo_id & time   │                             │    access controls)  │
                                    └──────────┬───────────┘                             └──────────┬───────────┘
                                               │                                                    │
                                               └──────────────────────────┬─────────────────────────┘
                                                                          │
                                                               ┌──────────▼──────────┐
                                                               │ SQLite (photos.db)  │
                                                               └─────────────────────┘
```

---

## 4. Database Schema Specifications

Four new tables will be added to the SQLite database during migration.

### A. Users Table (`users`)
Stores user accounts, hashed credentials, roles, and access level.
```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',           -- 'admin' or 'user'
    access_level INTEGER NOT NULL DEFAULT 1,     -- Baseline clearance for Phase 2
    created_at TEXT NOT NULL,
    last_login TEXT
);
```

### B. Per-User Settings Table (`user_settings`)
Allows each user to store independent display and UI preferences.
```sql
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
```

### C. Change Audit Log Table (`audit_log`)
Maintains an immutable record of catalog changes.
```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,                        -- 'mark_removal', 'unmark_removal', 'add_tag', 'remove_tag', 'edit_description'
    photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
    photo_filename TEXT,
    details TEXT                                 -- JSON or summary of changes
);
CREATE INDEX IF NOT EXISTS idx_audit_photo ON audit_log(photo_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);
```

### D. Tag Permissions Table (`tag_permissions`) *(Phase 2 Foundation)*
Defines clearance rules for restricted photo tags.
```sql
CREATE TABLE IF NOT EXISTS tag_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name TEXT UNIQUE NOT NULL,
    min_access_level INTEGER NOT NULL DEFAULT 1,
    restricted_to_role TEXT                       -- Optional role override (e.g., 'admin')
);
```

---

## 5. Authentication & Access Control

### 1. Password Security
- Passwords hashed using `werkzeug.security.generate_password_hash` (PBKDF2/scrypt).
- Passwords verified via `werkzeug.security.check_password_hash`.
- No plain-text passwords stored. Zero external dependencies required.

### 2. Session Management
- Sessions signed using Flask's `session` mechanism.
- A persistent `SECRET_KEY` generated once and stored in `.secret_key` on the server to prevent logging users out on server restart.

### 3. Route Protection (`before_request`)
- Global protection middleware:
  - Any request to `/`, `/image/<id>`, `/thumb/<id>`, `/api/...`, or `/settings` checks `session.get("user_id")`.
  - If unauthenticated:
    - HTML requests redirect to `/login?next=<path>`.
    - API / AJAX requests receive HTTP `401 Unauthorized`.
  - Whitelist: `/login`, `/logout`, `/static/...`.

### 4. CLI User Administration
CLI subcommands added to `app.py`:
- `python3 app.py --create-admin <username>`: Prompts for password and creates an admin user.
- `python3 app.py --create-user <username>`: Prompts for password and role (`admin` or `user`).
- `python3 app.py --reset-password <username>`: Prompts for a new password.
- `python3 app.py --list-users`: Lists all users, roles, and creation dates.

---

## 6. Change Auditing Implementation

### Mutation Interception
Every catalog change endpoint will automatically log the mutation into `audit_log`:

1. **`toggle_removal(photo_id)`**:
   - Action: `mark_removal` or `unmark_removal`.
   - Records: Photo ID, filename, author username, timestamp.
2. **`add_tag(photo_id)`**:
   - Action: `add_tag`.
   - Records: Photo ID, filename, tag name, author username, timestamp.
3. **`remove_tag(photo_id, tag_id)`**:
   - Action: `remove_tag`.
   - Records: Photo ID, filename, tag name, author username, timestamp.
4. **`update_description(photo_id)`**:
   - Action: `edit_description`.
   - Records: Photo ID, filename, previous description, updated description, author username, timestamp.

### Audit Log UI
- An **Audit History** view accessible from the Settings page or via `/audit`.
- Displays a chronological feed of recent operations:
  - Date & Time
  - User
  - Action badge (e.g. `Marked for Removal`, `Added Tag`, `Updated Description`)
  - Target Photo (filename, with link to full view)
  - Change details
- Accessible to all logged-in users.

---

## 7. Per-User Configuration System

1. **Default Settings Fallback**:
   - `DEFAULT_SETTINGS` provides the baseline system defaults.
   - When a user loads settings:
     `effective_settings = {**DEFAULT_SETTINGS, **user_overrides}`
2. **Configurable Options Per User**:
   - `dark_mode`: `"0"` (Light) or `"1"` (Dark).
   - `per_page`: `10`, `20`, `30`, or `50` photos per page.
   - `count_style`: `"column"` (Monospace straight right margin) or `"parens"` (`tag (count)`).
   - `all_tags_count`: `"1"` (Include total in "All tags") or `"0"` (Plain "All tags").
   - `show_zero_tags`: `"0"` (Hide 0-count tags) or `"1"` (Show 0-count tags).
3. **Settings Isolation**:
   - User A can browse in Dark Mode with 20 items per page.
   - User B can browse in Light Mode with 50 items per page.
   - User preferences persist across sessions and devices.

---

## 8. Future Tag-Based Authorization (Phase 2 Design)

To prepare for restricting access based on tags (e.g., `'level 1'` photos only accessible by users with `'level 1'` access):

1. **Query-Level Enforcement**:
   - Centralize photo queries into an authorized query builder:
     ```python
     def get_authorized_photo_filter(user):
         # If admin, full access
         if user["role"] == "admin":
             return "", []
         # Otherwise filter out photos containing restricted tags above user access level
         return """
             NOT EXISTS (
                 SELECT 1 FROM photo_tags pt
                 JOIN tags t ON pt.tag_id = t.id
                 JOIN tag_permissions tp ON t.name = tp.tag_name
                 WHERE pt.photo_id = p.id AND tp.min_access_level > ?
             )
         """, [user["access_level"]]
     ```
2. **Direct Media File Protection**:
   - `/image/<photo_id>` and `/thumb/<photo_id>` check authorization before reading/sending files:
     ```python
     if not user_can_access_photo(g.current_user, photo_id):
         abort(403)
     ```
3. **Tag & Dropdown Count Protection**:
   - Tag dropdown counts and stats badges calculate metrics exclusively using photos the logged-in user is authorized to see.

---

## 9. Phased Rollout Plan

### Phase 1: Authentication, Per-User Settings & Auditing
1. Schema migration (`users`, `user_settings`, `audit_log`, `tag_permissions`).
2. CLI commands (`--create-admin`, `--create-user`, `--reset-password`, `--list-users`).
3. Login & Logout views, authentication middleware (`before_request`), and persistent session security.
4. Per-user settings migration and user profile management.
5. Mutation audit logging in all photo modification routes.
6. Audit log viewer UI (accessible to all logged-in users).
7. Admin User Management panel (for admins to create/manage user accounts).

### Phase 2: Tag-Based Authorization Controls
1. Tag permissions management UI (Admins designate access levels / restrictions to specific tags).
2. Authorization filtering in photo grid queries (`index()`).
3. Authorization checks on `/image/<id>` and `/thumb/<id>` endpoints.
4. Access-restricted count calculations for filters and summary badges.
