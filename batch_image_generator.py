# ============================================================
# FILE: batch_image_generator.py
# CHANGE: generation_worker and verification_worker now wrap their
# per-item work in try/except. Previously, ANY unexpected exception
# (e.g. brain_core missing a function) killed that worker thread
# PERMANENTLY — with 5 verify workers, one repeating bug could kill
# all 5, leaving nothing to ever finalize another scene for the rest
# of the run. Now a crash fails just that one scene and the thread
# keeps running.
#
# CHANGE 2: scene state now also carries an optional LOCATION and
# PROPS list (parsed by image_core.load_scenes) so generation and
# verification can pull in those reference images too. The gen_queue
# / verify_queue tuple SHAPES are unchanged — location/props travel
# via scene_state (same place extra_instruction already lives), not
# through the queues, so retries/escalation paths didn't need to
# change at all.
# ============================================================

import queue
import threading

# Settle which run this stage works on BEFORE importing image_core, which
# resolves its paths at import time. Bare -> newest run. `... RUN-0009` -> that run.
import run_paths
run_paths.bootstrap_stage(__import__("sys").argv)

from image_core import (
    GEN_KEYS, VERIFY_KEYS, load_scenes, generate_image_url, download_image,
    verify_image, verification_passed, update_manifest_entry, load_manifest,
    classify_error, OUTPUT_FOLDER, SCENES_FILE,
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

def init_scene_state(scene_key, scene_text, location=None, props=None):
    with state_lock:
        scene_state[scene_key] = {
            "scene_text": scene_text,
            "extra_instruction": "",
            "location": location,
            "props": props or [],
        }

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

        # Everything below is wrapped — an unexpected crash here fails
        # ONLY this scene and lets the thread keep running, instead of
        # silently killing this entire lane for the rest of the run.
        try:
            state = get_scene_state(scene_key)
            board.update(scene_key, "generating")

            try:
                url = generate_image_url(
                    lane, characters, state["scene_text"], state["extra_instruction"],
                    location=state.get("location"), props=state.get("props"),
                )
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

            result, call_error = verify_image(
                lane, url, characters, state["scene_text"],
                location=state.get("location"), props=state.get("props"),
            )
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
                    "location": state.get("location"), "props": state.get("props"),
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
    import itertools

    scenes = load_scenes(SCENES_FILE)
    manifest = load_manifest()

    all_scene_keys = [f"scene_{n:03d}" for n, _, _, _, _ in scenes]
    total = len(all_scene_keys)

    to_queue = []
    already_done = 0
    for scene_number, characters, scene_text, location, props in scenes:
        scene_key = f"scene_{scene_number:03d}"
        if manifest.get(scene_key, {}).get("status") == "ok":
            already_done += 1
            continue
        to_queue.append((scene_number, characters, scene_text, location, props))

    if already_done:
        print(f"⏭️  {already_done}/{total} scenes already complete — skipping, resuming the rest")

    if not to_queue:
        print(f"✅ All {total} scenes already complete. Nothing to do.")
        return

    scene_keys = []
    for scene_number, characters, scene_text, location, props in to_queue:
        scene_key = f"scene_{scene_number:03d}"
        init_scene_state(scene_key, scene_text, location=location, props=props)
        gen_queue.put((scene_number, characters, scene_text))
        scene_keys.append(scene_key)

    register_total(len(scene_keys))
    verify_lane_cycle = itertools.cycle(VERIFY_KEYS)
    board = StatusBoard(scene_keys)

    print(f"✅ {len(scene_keys)} scene(s) queued — {len(GEN_KEYS)} gen lane(s), {len(VERIFY_KEYS)} verify lane(s)\n")

    gen_threads = [threading.Thread(target=generation_worker, args=(lane,), daemon=True) for lane in GEN_KEYS]
    verify_threads = [threading.Thread(target=verification_worker, daemon=True) for _ in VERIFY_KEYS]

    board.start()
    for t in gen_threads + verify_threads:
        t.start()

    all_done_event.wait()
    board.stop()

    manifest = load_manifest()
    this_run = {k: v for k, v in manifest.items() if k in all_scene_keys}
    ok_count = sum(1 for v in this_run.values() if v.get("status") == "ok")
    failed_count = sum(1 for v in this_run.values() if v.get("status") == "failed")

    print(f"\n✅ Successful: {ok_count} / {total}")
    print(f"❌ Failed: {failed_count} / {total}\n")

    if failed_count:
        for k, v in this_run.items():
            if v.get("status") == "failed":
                cat = v.get("error_category", "?")
                print(f"  {k} [{cat}]: {v.get('error', 'unknown')[:100]}")
        print("\nRun: python retry_images.py --all\n")

    pattern_watcher.analyze(manifest, all_scene_keys)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped — progress saved to manifest.json.")