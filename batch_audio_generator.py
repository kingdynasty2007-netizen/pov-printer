# ============================================================
# FILE: batch_audio_generator.py
# CHANGE: skips any scene that already has successful audio.
# RUN WITH: python batch_audio_generator.py
# ============================================================

import queue
import threading

# Settle which run this stage works on BEFORE importing audio_core, which
# resolves its paths at import time. Bare -> newest run. `... RUN-0009` -> that run.
import sys
import run_paths
run_paths.bootstrap_stage(sys.argv)

from audio_core import (
    GEMINI_KEYS, load_narration, generate_audio_once, load_manifest,
    update_manifest_entry, classify_error, MAX_RETRIES,
)
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


def main():
    global board

    print("\n=== BATCH AUDIO GENERATOR (Gemini TTS) ===\n")

    entries = load_narration()
    manifest = load_manifest()

    already_done = [e for e in entries if manifest.get(e[0], {}).get("audio", {}).get("status") == "ok"]
    to_queue = [e for e in entries if manifest.get(e[0], {}).get("audio", {}).get("status") != "ok"]

    if already_done:
        print(f"⏭️  {len(already_done)}/{len(entries)} scenes already have audio — skipping")

    if not to_queue:
        print("✅ All scenes already have audio. Nothing to do.")
        return

    scene_keys = []
    for scene_key, voice, text in to_queue:
        work_queue.put((scene_key, voice, text))
        scene_keys.append(scene_key)

    register_total(len(to_queue))
    board = StatusBoard(scene_keys)

    print(f"✅ {len(to_queue)} narration line(s) queued — {len(GEMINI_KEYS)} key(s): {', '.join(GEMINI_KEYS)}\n")

    threads = [threading.Thread(target=worker, args=(lane,), daemon=True) for lane in GEMINI_KEYS]

    board.start()
    for t in threads:
        t.start()

    all_done_event.wait()
    board.stop()

    manifest = load_manifest()
    successful = sum(1 for k in scene_keys if manifest.get(k, {}).get("audio", {}).get("status") == "ok")
    failed = [k for k in scene_keys if manifest.get(k, {}).get("audio", {}).get("status") == "failed"]

    print(f"\n✅ Successful: {successful}/{len(to_queue)}")
    if failed:
        print(f"❌ Failed: {', '.join(failed)}")
        print("Run: python retry_audio.py --all\n")
    else:
        print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped — progress already saved to manifest.json.")