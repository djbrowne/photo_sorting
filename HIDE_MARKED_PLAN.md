# Plan: Hide Photos Marked for Removal Configuration Setting

## 1. Executive Summary & Goals

This document specifies the technical design, architectural details, and implementation plan for adding a configurable setting that enables users to hide photos that have been marked for removal from standard catalog browsing.

### Core Objectives:
1. **Configurable Visibility**: Allow users to toggle whether photos marked for removal (`marked_for_removal = 1`) are visible in standard gallery views.
2. **Access When Filtered**: Preserve the ability to view, inspect, and toggle marked photos at any time by using the dedicated "marked for removal only" filter (`?marked=1`) or by clicking the header count badge.
3. **Count Consistency**: Harmonize the photo grid counts with tag dropdown counts and statistics badges (which already calculate totals excluding marked photos).
4. **Per-User & Server Isolation**: Support per-user preference storage in `user_settings` with smooth fallback to server defaults in `settings`.
5. **Portability & Export**: Ensure the setting is included in JSON configuration exports/imports and cross-database copying.

---

## 2. Key Architectural Decisions

1. **Configuration Key Name**:
   - `hide_marked_for_removal`
   - Explicit, unambiguous, and mirrors the SQLite database column name `marked_for_removal`.

2. **Default Setting Value**:
   - Default: `"0"` (Disabled by default).
   - Preserves 100% backward compatibility for existing users who are accustomed to seeing marked photos inline until they choose to enable the setting.

3. **Precedence Hierarchy**:
   - Direct user action overrides the hiding filter.
   - Specifically: when `marked_only = True` (i.e. `?marked=1` via the header badge or filter checkbox), marked photos are **always** displayed, regardless of whether `hide_marked_for_removal` is enabled.
   - When `marked_only = False` and `hide_marked_for_removal == "1"`, photos with `p.marked_for_removal = 0` are filtered.

4. **Client-Side UX on Removal Toggle**:
   - When a user checks "mark for removal" on a photo card while `hide_marked_for_removal` is active, the card receives the visual `.marked` style and updates header statistics immediately via AJAX, but remains on screen during the active browsing session.
   - This prevents the card from disappearing abruptly from under the user's cursor and allows immediate undo of accidental clicks. The card is excluded upon the next page navigation, filter submission, or reload.

---

## 3. Architecture & Data Flow

```
┌───────────────────────────────────────────────────────────────┐
│                      templates/settings.html                  │
│       Catalog Display: [x] Hide photos marked for removal     │
└───────────────────────────────┬───────────────────────────────┘
                                │ POST /settings
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                          app.py                               │
│  1. DEFAULT_SETTINGS["hide_marked_for_removal"] = "0"         │
│  2. settings_page(): save to user_settings (or settings)      │
│  3. settings_export() / import: auto-handled via key registry │
│  4. index():                                                  │
│        if marked_only:                                        │
│            where.append("p.marked_for_removal = 1")           │
│        elif hide_marked:                                      │
│            where.append("p.marked_for_removal = 0")           │
└───────────────────────────────┬───────────────────────────────┘
                                │ Evaluates WHERE clause
                                ▼
┌───────────────────────────────────────────────────────────────┐
│               photos.db (SQLite) & templates/index.html       │
│  - Grid excludes marked photos when setting is enabled        │
│  - Pagination total reflects active photos count              │
│  - Direct filter (?marked=1) displays marked photos           │
└───────────────────────────────────────────────────────────────┘
```

---

## 4. Component Details

### A. Settings Registry in `app.py`
Add `"hide_marked_for_removal": "0"` to `DEFAULT_SETTINGS`:
- Keys in `DEFAULT_SETTINGS` dictate which settings are recognized and persisted across `save_user_settings()` and `save_settings()`.
- Exporting settings (`/settings/export`) will serialize `hide_marked_for_removal`.
- Importing settings (`/settings/import-json` and `/settings/import-db`) will ingest `hide_marked_for_removal` seamlessly.

### B. Route Query in `app.py` (`index()`)
Query construction logic in `index()`:
```python
tag_filter = request.args.get("tag", "", type=str).strip()
marked_only = request.args.get("marked", "", type=str) == "1"
search = request.args.get("q", "", type=str).strip()
hide_marked = settings.get("hide_marked_for_removal") == "1"

where, params, join = [], [], ""
if tag_filter:
    join = "JOIN photo_tags pt ON p.id = pt.photo_id JOIN tags t ON pt.tag_id = t.id"
    where.append("t.name = ?")
    params.append(tag_filter)
if marked_only:
    where.append("p.marked_for_removal = 1")
elif hide_marked:
    where.append("p.marked_for_removal = 0")
if search:
    where.append("p.description LIKE ?")
    params.append(f"%{search}%")
where_sql = ("WHERE " + " AND ".join(where)) if where else ""
```

### C. Settings Form in `templates/settings.html`
Add a dedicated "Catalog Display" card:
```html
<div class="settings-card">
  <h2>Catalog Display</h2>
  <div class="form-row">
    <label class="checkbox-label">
      <input type="checkbox" name="hide_marked_for_removal" value="1" {% if settings.hide_marked_for_removal == '1' %}checked{% endif %}>
      <span>Hide photos marked for removal</span>
    </label>
    <span class="field-help">When enabled, photos marked for removal are hidden from normal catalog views. You can view them at any time using the &ldquo;marked for removal&rdquo; filter.</span>
  </div>
</div>
```

---

## 5. Verification & Testing Strategy

1. **Default State**:
   - Verify that with fresh settings (`hide_marked_for_removal="0"`), all photos (marked and unmarked) are returned.
2. **Hiding Active**:
   - Update setting to `"1"`.
   - Verify that `/` returns only photos with `marked_for_removal = 0`.
   - Verify total count matches the unmarked count in the database.
3. **Explicit Marked Filter Override**:
   - Access `/?marked=1` while `hide_marked_for_removal="1"`.
   - Verify that marked photos are returned and accessible for editing.
4. **Tag & Search Combinations**:
   - Test `/?tag=<tag_name>`: only unmarked photos with that tag are returned.
   - Test `/?tag=<tag_name>&marked=1`: only marked photos with that tag are returned.
   - Test `/?q=<query>`: only unmarked photos matching description are returned.
5. **Settings Persistence & Migration**:
   - Verify saving settings from `/settings` form saves `"1"` when checked and `"0"` when unchecked.
   - Verify settings export produces valid JSON containing `hide_marked_for_removal`.
   - Verify settings import accepts and updates `hide_marked_for_removal`.
