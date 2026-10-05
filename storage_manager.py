# ============================================================
# FILE: storage_manager.py
# PURPOSE: All filesystem and SQLite operations for the manual
# production pipeline. Single source of truth for paths,
# directory creation, story/script versioning, JSON artifacts,
# and production state.
#
# CHANGE: added a "story" stage alongside "script" — the full prose
# story now gets its own versioned folder and its own current.txt,
# completely separate from the scene-breakdown ("script") versions,
# so the two never collide on the same v001/v002/... numbering.
#
# CHANGE 2: _generate_run_id() now takes the next unused number from
# BOTH the folder listing AND the productions table (whichever is
# higher) — previously it only looked at folders, so a folder/DB
# mismatch (e.g. after a crash) could hand out an ID that already
# existed in the DB and crash with sqlite3.IntegrityError.
# create_production() also retries a few times on a collision as a
# belt-and-suspenders safety net.
#
# Does NOT touch Supabase — that remains in db.py / script_engine.py.
# Does NOT store image/audio/video blobs — filesystem paths only.
# ============================================================

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "pov_printer.db")
PRODUCTIONS_DIR = os.path.join(DATA_DIR, "productions")

_db_lock = threading.Lock()


# ============================================================
# DATABASE SETUP
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS productions (
    id                  TEXT PRIMARY KEY,
    topic               TEXT NOT NULL,
    input_type          TEXT NOT NULL,
    format              TEXT NOT NULL,
    duration_secs       INTEGER NOT NULL,
    stage               TEXT NOT NULL DEFAULT 'created',
    script_status       TEXT,
    current_script_version TEXT,
    verification_status TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    error               TEXT,
    supabase_topic_id   TEXT,
    notes               TEXT
);
"""


def _get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call multiple times."""
    with _db_lock:
        conn = _get_conn()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


# ============================================================
# PRODUCTION ID
# ============================================================

def _generate_run_id():
    """
    Generate RUN-XXXX style ID. Takes the next unused number from BOTH
    the folder listing AND the productions table — whichever is
    higher — so a deleted folder or an orphaned DB row can never
    cause the two to pick the same number again.
    """
    os.makedirs(PRODUCTIONS_DIR, exist_ok=True)
    existing = [
        d for d in os.listdir(PRODUCTIONS_DIR)
        if d.startswith("RUN-") and os.path.isdir(os.path.join(PRODUCTIONS_DIR, d))
    ]
    folder_numbers = []
    for name in existing:
        try:
            folder_numbers.append(int(name[4:]))
        except ValueError:
            pass
    folder_max = max(folder_numbers, default=0)

    db_max = 0
    init_db()
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute("SELECT id FROM productions WHERE id LIKE 'RUN-%'").fetchall()
        finally:
            conn.close()
    for row in rows:
        try:
            db_max = max(db_max, int(row["id"][4:]))
        except (ValueError, TypeError):
            pass

    next_num = max(folder_max, db_max) + 1
    return f"RUN-{next_num:04d}"


# ============================================================
# DIRECTORY STRUCTURE
# ============================================================

def create_production_dirs(run_id):
    """
    Create the full directory tree for a production run.
    Returns a dict of all important paths.
    """
    root = os.path.join(PRODUCTIONS_DIR, run_id)
    dirs = {
        "root": root,
        "input": os.path.join(root, "input"),
        "research": os.path.join(root, "research"),
        "story": os.path.join(root, "story"),
        "story_versions": os.path.join(root, "story", "versions"),
        "script": os.path.join(root, "script"),
        "script_versions": os.path.join(root, "script", "versions"),
        "verification": os.path.join(root, "verification"),
        "breakdown": os.path.join(root, "breakdown"),
        "characters": os.path.join(root, "characters"),
        "references": os.path.join(root, "references"),
        "media": os.path.join(root, "media"),
        "media_images": os.path.join(root, "media", "images"),
        "media_audio": os.path.join(root, "media", "audio"),
        "media_video": os.path.join(root, "media", "video"),
        "metadata": os.path.join(root, "metadata"),
        "final": os.path.join(root, "final"),
        "logs": os.path.join(root, "logs"),
    }
    for path in dirs.values():
        os.makedirs(path, exist_ok=True)
    return dirs


# ============================================================
# PRODUCTION RECORD (SQLite)
# ============================================================

def _now():
    return datetime.now(timezone.utc).isoformat()


def create_production(topic, input_type, fmt, duration_secs, supabase_topic_id=None):
    """
    Create a new production record. Returns (run_id, paths_dict).
    input_type: 'topic' or 'existing_script'

    Retries a few times on a run_id collision (belt-and-suspenders on
    top of _generate_run_id's fix — e.g. two runs launched at nearly
    the same instant) rather than crashing the whole pipeline.
    """
    init_db()
    now = _now()
    last_error = None

    for attempt in range(5):
        run_id = _generate_run_id()
        paths = create_production_dirs(run_id)

        with _db_lock:
            conn = _get_conn()
            try:
                conn.execute(
                    """INSERT INTO productions
                       (id, topic, input_type, format, duration_secs, stage,
                        created_at, updated_at, supabase_topic_id)
                       VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?)""",
                    (run_id, topic, input_type, fmt, duration_secs,
                     now, now, supabase_topic_id),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                last_error = e
                conn.close()
                continue
            finally:
                conn.close()

        print(f"📁 Production created: {run_id}")
        return run_id, paths

    raise RuntimeError(f"Could not generate a unique run ID after 5 attempts: {last_error}")


def update_production(run_id, **kwargs):
    """Update any column(s) on a production record."""
    if not kwargs:
        return
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [run_id]
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute(f"UPDATE productions SET {cols} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()


def get_production(run_id):
    """Return a production record as a dict, or None."""
    init_db()
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM productions WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# ============================================================
# VERSIONING — shared helpers (used by both story and script)
# ============================================================

def _version_label(n):
    return f"v{n:03d}"


def _next_version_number(versions_dir):
    existing = [
        f for f in os.listdir(versions_dir)
        if f.startswith("v") and f.endswith(".txt")
    ]
    numbers = []
    for name in existing:
        try:
            numbers.append(int(name[1:-4]))
        except ValueError:
            pass
    return max(numbers, default=0) + 1


# ============================================================
# STORY VERSIONING (the full prose story — upstream of the script)
# ============================================================

def save_story_version(paths, story_text):
    """
    Save story_text as the next STORY version (v001, v002, …), in its
    OWN versions folder — never shares numbering with script versions.
    Returns the version label (e.g. 'v001').
    """
    versions_dir = paths["story_versions"]
    n = _next_version_number(versions_dir)
    label = _version_label(n)
    version_path = os.path.join(versions_dir, f"{label}.txt")
    with open(version_path, "w", encoding="utf-8") as f:
        f.write(story_text)
    print(f"💾 Story saved as {label}")
    return label


def set_current_story(paths, story_text):
    """Write (or overwrite) story/current.txt — only called after PASS."""
    current_path = os.path.join(paths["story"], "current.txt")
    with open(current_path, "w", encoding="utf-8") as f:
        f.write(story_text)


def load_story_version(paths, label):
    """Load a specific story version by label (e.g. 'v001')."""
    path = os.path.join(paths["story_versions"], f"{label}.txt")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Story version {label} not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_story_versions(paths):
    """Return sorted list of story version labels that exist."""
    versions_dir = paths["story_versions"]
    files = [f for f in os.listdir(versions_dir) if f.startswith("v") and f.endswith(".txt")]
    return sorted(files, key=lambda x: int(x[1:-4]))


# ============================================================
# SCRIPT VERSIONING (the scene breakdown — derived from the story)
# ============================================================

def save_script_version(paths, script_text):
    """
    Save script_text as the next version (v001, v002, …).
    NEVER overwrites an existing version.
    Returns the version label (e.g. 'v001').
    """
    versions_dir = paths["script_versions"]
    n = _next_version_number(versions_dir)
    label = _version_label(n)
    version_path = os.path.join(versions_dir, f"{label}.txt")
    with open(version_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    print(f"💾 Script saved as {label}")
    return label


def set_current_script(paths, script_text):
    """Write (or overwrite) current.txt — only called after PASS."""
    current_path = os.path.join(paths["script"], "current.txt")
    with open(current_path, "w", encoding="utf-8") as f:
        f.write(script_text)


def load_script_version(paths, label):
    """Load a specific version by label (e.g. 'v001')."""
    path = os.path.join(paths["script_versions"], f"{label}.txt")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Script version {label} not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_script_versions(paths):
    """Return sorted list of version labels that exist."""
    versions_dir = paths["script_versions"]
    files = [f for f in os.listdir(versions_dir) if f.startswith("v") and f.endswith(".txt")]
    return sorted(files, key=lambda x: int(x[1:-4]))


# ============================================================
# JSON ARTIFACTS
# ============================================================

def save_json(paths, subfolder_key, filename, data):
    """
    Save a JSON artifact.
    subfolder_key: key from paths dict (e.g. 'research', 'breakdown', 'metadata')
    """
    folder = paths[subfolder_key]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def load_json(paths, subfolder_key, filename):
    """Load a JSON artifact. Returns None if not found."""
    folder = paths[subfolder_key]
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_text(paths, subfolder_key, filename, text):
    """Save a plain text artifact."""
    folder = paths[subfolder_key]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ============================================================
# VERIFICATION RESULTS
# ============================================================

def save_verification(paths, version_label, result):
    """Save a verification JSON result for a specific script version."""
    return save_json(paths, "verification", f"{version_label}.json", result)


def load_verification(paths, version_label):
    """Load a verification result for a specific script version."""
    return load_json(paths, "verification", f"{version_label}.json")


# ============================================================
# PRODUCTION MANIFEST (per-run)
# ============================================================

def save_production_manifest(paths, data):
    """Save the per-production manifest.json."""
    path = os.path.join(paths["root"], "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def load_production_manifest(paths):
    """Load the per-production manifest.json. Returns {} if missing."""
    path = os.path.join(paths["root"], "manifest.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else {}