# ============================================================
# FILE: retry_images.py
# CHANGE: same worker-crash resilience as batch_image_generator.py.
# RUN WITH:
#   python retry_images.py 3 7 10
#   python retry_images.py --all
# ============================================================

import sys
import re
import queue
import threading
import itertools
from image_core import (
    GEN_KEYS, VERIFY_KEYS, load_scenes, generate_image_url, download_image,
    verify_image, verification_passed, update_manifest_entry, load_manifest,
    classify_error, get_current_scene_keys, OUTPUT_FOLDER, SCENES_FILE, FRAMES_FILE,
    MAX_BLIND_RETRIES, MAX_BRAIN_RETRIES,
)
import brain_core
import pattern_watcher
from known_fixes import check_known_fixes
from status_board import StatusBoard

gen_queue = queue.Queue()
verify_queue = queue.Queue()

scene_state = {}
state_lock = threading.Lock()

def init_scene_state(scene_key, scene_text):
    with state_lock:
        scene_state[scene_key] = {"scene_text": scene_text, "extra_instruction": ""}

def get_scene_state(scene_key):
    with state_lock:
        return dict(scene_state[scene_key])

def apply_fix(scene_key, fix):
    with state_lock:
        if fix["strategy"] == "patch":
            scene_state[scene_key]["extra_instruction"] = fix["extra_instruction"]
        elif fix["strategy"] == "rewrite":
            scene_state[scene_key]["scene_text"] = fix["scene_text"]
            scene_state[scene_key]["extra_instruction"] = ""

retry_state = {}
retry_lock = threading.Lock()

def bump_blind(scene_key):
    with retry_lock:
        retry_state.setdefault(scene_key, {"blind": 0, "brain": 0})
        retry_state[scene_key]["blind"] += 1
        return retry_state[scene_key]["blind"]

def bump_brain(scene_key):
    with retry_lock:
        retry_state.setdefault(scene_key, {"blind": 0, "brain": 0})
        retry_state[scene_key]["brain"] += 1
        return retry_state[scene_key]["brain"]

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
verify_lane_cycle = None
verify_lane_lock = threading.Lock()

def next_verify_lane():
    with verify_lane_lock:
        return next(verify_lane_cycle)


def find_failed_numbers(prefix, current_keys):
    manifest = load_manifest()
    pattern = re.compile(rf"^{prefix}_(\d+)$")
    failed = []
    skipped_stale = 0
    for key, entry in manifest.items():
        if entry.get("status") == "failed":
            if key not in current_keys:
                skipped_stale += 1
                continue
            match = pattern.match(key)
            if match:
                failed.append(int(match.group(1)))
    if skipped_stale:
        print(f"ℹ️  Ignored {skipped_stale} failed entr(y/ies) not part of the current story")
    return sorted(failed)


def finalize_failed(scene_key, category, reason, unverified_url=None):
    update = {"status": "failed", "error": reason, "error_category": category}
    if unverified_url:
        update["unverified_url"] = unverified_url
    update_manifest_entry(scene_key, update)
    board.update(scene_key, f"[FAILED:{category}] {reason[:60]}", done=True)
    mark_finalized()


def escalate(scene_number, scene_key, characters, issue):
    known_fix, known_name = check_known_fixes(issue)
    if known_fix:
        apply_fix(scene_key, {"strategy": "patch", "extra_instruction": known_fix})
        board.update(scene_key, f"[KNOWN-FIX:{known_name}] applied")
        gen_queue.put((scene_number, characters, None))
        return

    n = bump_brain(scene_key)
    if n > MAX_BRAIN_RETRIES:
        finalize_failed(scene_key, "brain_exhausted", issue)
        return

    current = get_scene_state(scene_key)
    board.update(scene_key, f"[BRAIN {n}/{MAX_BRAIN_RETRIES}] diagnosing")

    def report(call_attempt, max_attempts, lane):
        board.update(scene_key, f"[BRAIN {n}/{MAX_BRAIN_RETRIES}] call {call_attempt}/{max_attempts} via {lane}")

    fix, brain_error = brain_core.get_fix(
        scene_key, characters, current["scene_text"], issue,
        attempt_number=n, previous_extra_instruction=current["extra_instruction"],
        on_attempt=report,
    )
    if fix is None:
        board.update(scene_key, f"[BRAIN ERROR] {brain_error}")
    else:
        apply_fix(scene_key, fix)
        board.update(scene_key, f"[BRAIN:{fix.get('category','?')}] {fix['strategy']} applied")
    gen_queue.put((scene_number, characters, None))


def generation_worker(lane):
    while not all_done_event.is_set():
        try:
            scene_number, characters, _ = gen_queue.get(timeout=2)
        except queue.Empty:
            continue

        scene_key = f"scene_{scene_number:03d}"

        try:
            state = get_scene_state(scene_key)
            board.update(scene_key, "generating")

            try:
                url = generate_image_url(lane, characters, state["scene_text"], state["extra_instruction"])
            except Exception as e:
                error_text = str(e)
                category = classify_error(error_text)

                if category == "content_policy":
                    escalate(scene_number, scene_key, characters, error_text)
                elif category == "transient":
                    n = bump_blind(scene_key)
                    if n <= MAX_BLIND_RETRIES:
                        board.update(scene_key, f"[RETRY {n}/{MAX_BLIND_RETRIES}:transient]")
                        gen_queue.put((scene_number, characters, None))
                    else:
                        finalize_failed(scene_key, "transient", error_text)
                else:
                    n = bump_blind(scene_key)
                    if n <= 1:
                        board.update(scene_key, f"[RETRY {n}/1:other]")
                        gen_queue.put((scene_number, characters, None))
                    else:
                        escalate(scene_number, scene_key, characters, error_text)
                continue

            verify_queue.put((scene_number, scene_key, characters, url))

        except Exception as e:
            n = bump_blind(scene_key)
            board.update(scene_key, f"[WORKER CRASH:gen] {type(e).__name__}: {str(e)[:60]}")
            if n <= MAX_BLIND_RETRIES:
                gen_queue.put((scene_number, characters, None))
            else:
                finalize_failed(scene_key, "worker_crash", f"{type(e).__name__}: {e}")


def verification_worker():
    while not all_done_event.is_set():
        try:
            scene_number, scene_key, characters, url = verify_queue.get(timeout=2)
        except queue.Empty:
            continue

        try:
            lane = next_verify_lane()
            state = get_scene_state(scene_key)
            board.update(scene_key, "verifying")

            result, call_error = verify_image(lane, url, characters, state["scene_text"])
            passed = verification_passed(result)

            if passed:
                output_path = f"{OUTPUT_FOLDER}/{scene_key}.png"
                try:
                    download_image(url, output_path)
                except Exception as e:
                    finalize_failed(scene_key, "download", str(e), unverified_url=url)
                    continue

                update_manifest_entry(scene_key, {
                    "url": url, "local_path": output_path,
                    "characters": characters, "scene_text": state["scene_text"],
                    "verified": True, "status": "ok",
                })
                board.update(scene_key, "verified & downloaded", done=True)
                mark_finalized()

            elif passed is None:
                n = bump_blind(scene_key)
                if n <= MAX_BLIND_RETRIES:
                    board.update(scene_key, f"[RETRY {n}/{MAX_BLIND_RETRIES}:verify_unreachable]")
                    verify_queue.put((scene_number, scene_key, characters, url))
                else:
                    finalize_failed(scene_key, "verify_unreachable", call_error or "unknown", unverified_url=url)

            else:
                issue = result.get("issue", "unknown")
                escalate(scene_number, scene_key, characters, issue)

        except Exception as e:
            n = bump_blind(scene_key)
            board.update(scene_key, f"[WORKER CRASH:verify] {type(e).__name__}: {str(e)[:60]}")
            if n <= MAX_BLIND_RETRIES:
                verify_queue.put((scene_number, scene_key, characters, url))
            else:
                finalize_failed(scene_key, "worker_crash", f"{type(e).__name__}: {e}")


def main():
    global board, verify_lane_cycle

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python retry_images.py 3 7 10")
        print("  python retry_images.py --all")
        sys.exit(1)

    use_frames = "--frames" in sys.argv
    auto_mode = "--all" in sys.argv
    file_path = FRAMES_FILE if use_frames else SCENES_FILE
    prefix = "frame" if use_frames else "scene"

    current_keys = get_current_scene_keys() if not use_frames else set()

    if auto_mode:
        scene_numbers = find_failed_numbers(prefix, current_keys)
        if not scene_numbers:
            print(f"✅ No failed {prefix}s found in the current story.")
            return
        print(f"🔍 Auto-detected {len(scene_numbers)} failed {prefix}(s): {scene_numbers}")
    else:
        scene_numbers = [int(a) for a in sys.argv[1:] if a.isdigit()]
        if not scene_numbers:
            print("❌ No valid scene numbers given, and --all wasn't used.")
            sys.exit(1)

    all_scenes = load_scenes(file_path)
    lookup = {number: (chars, text) for number, chars, text in all_scenes}

    scene_keys = []
    for n in scene_numbers:
        if n not in lookup:
            print(f"❌ #{n} not found in {file_path} — skipping.")
            continue
        chars, text = lookup[n]
        scene_key = f"{prefix}_{n:03d}"
        init_scene_state(scene_key, text)
        gen_queue.put((n, chars, text))
        scene_keys.append(scene_key)

    if not scene_keys:
        print("❌ No valid scenes to retry.")
        return

    register_total(len(scene_keys))
    verify_lane_cycle = itertools.cycle(VERIFY_KEYS)
    board = StatusBoard(scene_keys)

    print(f"\n🔁 Retrying {len(scene_keys)} scene(s) — {len(GEN_KEYS)} gen lane(s), {len(VERIFY_KEYS)} verify lane(s)\n")

    gen_threads = [threading.Thread(target=generation_worker, args=(lane,), daemon=True) for lane in GEN_KEYS]
    verify_threads = [threading.Thread(target=verification_worker, daemon=True) for _ in VERIFY_KEYS]

    board.start()
    for t in gen_threads + verify_threads:
        t.start()

    all_done_event.wait()
    board.stop()

    manifest = load_manifest()
    still_failed = [k for k in scene_keys if manifest.get(k, {}).get("status") == "failed"]

    print(f"\n✅ Retry batch complete")
    if still_failed:
        print(f"❌ Still failed: {', '.join(still_failed)}")
    else:
        print("🎉 All retried scenes now passed.")

    pattern_watcher.analyze(manifest, scene_keys)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped — progress saved to manifest.json.")