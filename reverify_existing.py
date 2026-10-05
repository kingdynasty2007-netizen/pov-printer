# ============================================================
# FILE: reverify_existing.py
# PURPOSE: Re-check already-downloaded "ok" images against the
# hardened verification (pose_action_ok, stricter stretching check,
# position-identity check) — catches anything the OLDER, weaker
# verification let through before these fixes existed.
#
# Re-verifies from the LOCAL FILE, not the original Agnes URL (which
# may have expired) — fully independent of remote state.
#
# On a catch:
#   - renames the file to {scene_key}_fix.png (visual audit trail)
#   - if this scene has been caught 3+ times total (persists across
#     runs), marks it "needs_human_review" instead of looping forever
#   - otherwise marks "failed" so retry_images.py --all fixes it
#
# Flag-only — does not regenerate anything itself.
# RUN WITH: python reverify_existing.py
# ============================================================

import os
import itertools
import sys

# Settle which run this stage works on BEFORE importing image_core, which
# resolves its paths at import time. Bare -> newest run. `... RUN-0009` -> that run.
import run_paths
run_paths.bootstrap_stage(sys.argv)

from image_core import (
    VERIFY_KEYS, load_manifest, update_manifest_entry, get_current_scene_keys,
    verify_image, verification_passed, local_image_to_data_uri,
)
from status_board import StatusBoard

MAX_AUDIT_CATCHES_BEFORE_HUMAN = 3


def main():
    print("\n=== RE-VERIFY EXISTING IMAGES (hardened check) ===\n")

    manifest = load_manifest()
    current_keys = get_current_scene_keys()

    to_check = [
        (key, entry) for key, entry in manifest.items()
        if key in current_keys and entry.get("status") == "ok" and entry.get("local_path")
        and os.path.exists(entry["local_path"])
    ]

    if not to_check:
        print("❌ No verified images found for the current story to re-check.")
        return

    print(f"🔍 Re-checking {len(to_check)} previously-verified image(s)...\n")

    board = StatusBoard([k for k, _ in to_check])
    board.start()

    lane_cycle = itertools.cycle(VERIFY_KEYS)
    caught = []
    clean = 0

    for scene_key, entry in to_check:
        lane = next(lane_cycle)
        board.update(scene_key, "re-verifying")

        characters = entry.get("characters", [])
        scene_text = entry.get("scene_text", "")
        local_path = entry["local_path"]

        try:
            data_uri = local_image_to_data_uri(local_path)
        except Exception as e:
            board.update(scene_key, f"[SKIP] could not read local file: {e}", done=True)
            continue

        result, call_error = verify_image(lane, data_uri, characters, scene_text)
        passed = verification_passed(result)

        if passed:
            board.update(scene_key, "clean — no change", done=True)
            clean += 1
            continue

        if passed is None:
            board.update(scene_key, f"[SKIP] re-check unreachable: {call_error}", done=True)
            continue

        issue = result.get("issue", "unknown")
        audit_catches = entry.get("audit_catch_count", 0) + 1

        base, ext = os.path.splitext(local_path)
        flagged_path = f"{base}_fix{ext}"
        try:
            if os.path.exists(flagged_path):
                os.remove(flagged_path)
            os.rename(local_path, flagged_path)
        except Exception:
            flagged_path = local_path

        if audit_catches >= MAX_AUDIT_CATCHES_BEFORE_HUMAN:
            update_manifest_entry(scene_key, {
                "status": "needs_human_review",
                "error": issue,
                "audit_catch_count": audit_catches,
                "flagged_path": flagged_path,
            })
            board.update(scene_key, f"[NEEDS HUMAN REVIEW] caught {audit_catches}x: {issue[:50]}", done=True)
        else:
            update_manifest_entry(scene_key, {
                "status": "failed",
                "error": issue,
                "audit_catch_count": audit_catches,
                "flagged_path": flagged_path,
            })
            board.update(scene_key, f"[FLAGGED {audit_catches}/{MAX_AUDIT_CATCHES_BEFORE_HUMAN}] {issue[:50]}", done=True)

        caught.append((scene_key, audit_catches, issue))

    board.stop()

    print(f"\n✅ Clean: {clean} / {len(to_check)}")
    print(f"🚩 Caught: {len(caught)} / {len(to_check)}\n")

    if caught:
        needs_human = [c for c in caught if c[1] >= MAX_AUDIT_CATCHES_BEFORE_HUMAN]
        needs_retry = [c for c in caught if c[1] < MAX_AUDIT_CATCHES_BEFORE_HUMAN]

        if needs_retry:
            print("Flagged, will auto-fix on next retry run:")
            for key, count, issue in needs_retry:
                print(f"  {key} (catch {count}/{MAX_AUDIT_CATCHES_BEFORE_HUMAN}): {issue[:80]}")
            print("\nRun: python retry_images.py --all\n")

        if needs_human:
            print("⚠️  NEEDS HUMAN REVIEW (caught repeatedly, auto-retry stopped):")
            for key, count, issue in needs_human:
                print(f"  {key}: {issue[:80]}")
            print("\nThese will NOT auto-regenerate. Check the *_fix files and decide")
            print("manually — likely needs a real scene-text or reference-image edit.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")