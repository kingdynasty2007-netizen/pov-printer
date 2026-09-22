# ============================================================
# FILE: run_lock.py
# Lets manual_runner.py see whether topic_queue.py's automatic
# background run is active, and request it stop. Pure filesystem
# coordination — no sockets, no shared process.
# ============================================================

import os
import json
import time

STATUS_FILE = "pipeline_status.json"
STOP_FLAG_FILE = "pipeline_stop_requested.flag"

# If the status file hasn't been touched in this long, treat the
# background run as dead (crashed without cleaning up) instead of
# blocking manual runs forever on a stale lock.
STALE_AFTER_SECONDS = 5 * 3600


def mark_started(topic_id, topic_text):
    _write({
        "running": True,
        "topic_id": topic_id,
        "topic_text": topic_text,
        "stage": "starting",
        "started_at": time.time(),
        "last_updated": time.time(),
    })
    clear_stop_request()


def mark_stage(stage_name):
    status = _read()
    if status is None:
        return
    status["stage"] = stage_name
    status["last_updated"] = time.time()
    _write(status)


def mark_finished():
    status = _read() or {}
    status["running"] = False
    status["stage"] = "finished"
    status["last_updated"] = time.time()
    _write(status)


def get_background_status():
    return _read()


def is_background_running():
    status = _read()
    if status is None or not status.get("running"):
        return False
    if time.time() - status.get("last_updated", 0) > STALE_AFTER_SECONDS:
        return False   # stale lock — don't block manual runs on a crashed process
    return True


def request_stop():
    with open(STOP_FLAG_FILE, "w") as f:
        f.write(str(time.time()))


def stop_requested():
    return os.path.exists(STOP_FLAG_FILE)


def clear_stop_request():
    if os.path.exists(STOP_FLAG_FILE):
        os.remove(STOP_FLAG_FILE)


def _read():
    if not os.path.exists(STATUS_FILE):
        return None
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else None
    except Exception:
        return None


def _write(status):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)