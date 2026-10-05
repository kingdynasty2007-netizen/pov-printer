# ============================================================
# FILE: retry_audio.py
# RUN WITH:
#   python retry_audio.py scene_003 scene_007
#   python retry_audio.py --all
# ============================================================

import sys

# Settle which run this stage works on BEFORE importing audio_core, which
# resolves its paths at import time. Bare -> newest run. `... RUN-0009` -> that run.
import run_paths
run_paths.bootstrap_stage(sys.argv)

from audio_core import (
    GEMINI_KEYS, load_narration, generate_audio_once, load_manifest,
    update_manifest_entry, classify_error, MAX_RETRIES,
)
import queue
import threading
from status_board import StatusBoard

work_queue = queue.Queue()
retry_counts = {}
retry_lock = threading.Lock()

def bump_retry(scene_key):
    with retry_lock:
        retry_counts[scene_key] = retry_counts.get(scene_key, 0) + 1
        return retry_counts[scene_key]

_outstanding_lock = threading.Lock()
outstanding = 0
all_done_event = threading.Event()

def register_total(n):
    global outstanding
    with _outstanding_lock:
        outstanding = n
        if outstanding <= 0:
            all_done_event.set()

def mark_finalized():
    global outstanding
    with _outstanding_lock:
        outstanding -= 1
        if outstanding <= 0:
            all_done_event.set()

board = None


def worker(lane):
    while not all_done_event.is_set():
        try:
            scene_key, voice, text = work_queue.get(timeout=2)
        except queue.Empty:
            continue

        attempt = retry_counts.get(scene_key, 0) + 1
        board.update(scene_key, f"generating (voice={voice}, attempt {attempt}/{MAX_RETRIES})")

        try:
            output_path = generate_audio_once(lane, scene_key, voice, text)
            update_manifest_entry(scene_key, {
                "audio": {"local_path": output_path, "voice": voice, "status": "ok"}
            })
            board.update(scene_key, "✅ saved", done=True)
            mark_finalized()
        except Exception as e:
            error_text = str(e)
            category = classify_error(error_text)
            n = bump_retry(scene_key)
            if n < MAX_RETRIES:
                board.update(scene_key, f"failed ({category}), retrying: {error_text[:50]}")
                work_queue.put((scene_key, voice, text))
            else:
                update_manifest_entry(scene_key, {"audio": {"status": "failed", "error": error_text}})
                board.update(scene_key, f"❌ FAILED: {error_text[:60]}", done=True)
                mark_finalized()


def find_failed_or_missing(all_entries, manifest):
    to_retry = []
    for scene_key, (voice, text) in all_entries.items():
        audio = manifest.get(scene_key, {}).get("audio")
        if audio is None or audio.get("status") != "ok":
            to_retry.append((scene_key, voice, text))
    return to_retry


def main():
    global board

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python retry_audio.py scene_003 scene_007")
        print("  python retry_audio.py --all")
        sys.exit(1)

    all_entries = {key: (voice, text) for key, voice, text in load_narration()}
    manifest = load_manifest()

    if sys.argv[1] == "--all":
        to_process = find_failed_or_missing(all_entries, manifest)
        if not to_process:
            print("✅ Nothing to retry — every scene already has successful audio.")
            return
        print(f"🔍 Found {len(to_process)} scene(s) needing retry: {', '.join(k for k, _, _ in to_process)}")
    else:
        wanted_keys = sys.argv[1:]
        to_process = []
        for scene_key in wanted_keys:
            if scene_key not in all_entries:
                print(f"❌ {scene_key} not found in audio_scenes.txt — skipping")
                continue
            voice, text = all_entries[scene_key]
            to_process.append((scene_key, voice, text))

    if not to_process:
        print("❌ No valid scenes to retry.")
        return

    scene_keys = [k for k, _, _ in to_process]
    for item in to_process:
        work_queue.put(item)

    register_total(len(to_process))
    board = StatusBoard(scene_keys)

    print(f"\n🔁 Retrying {len(to_process)} narration line(s) — {len(GEMINI_KEYS)} key(s): {', '.join(GEMINI_KEYS)}\n")

    threads = [threading.Thread(target=worker, args=(lane,), daemon=True) for lane in GEMINI_KEYS]

    board.start()
    for t in threads:
        t.start()

    all_done_event.wait()
    board.stop()

    manifest = load_manifest()
    still_failed = [k for k in scene_keys if manifest.get(k, {}).get("audio", {}).get("status") == "failed"]

    print(f"\n✅ Retry batch complete")
    if still_failed:
        print(f"❌ Still failed: {', '.join(still_failed)}")
    else:
        print("🎉 All retried scenes now have audio.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped — progress saved to manifest.json.")