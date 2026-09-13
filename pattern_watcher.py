# ============================================================
# FILE: pattern_watcher.py
# PURPOSE: After a batch run, scan every failure and detect if the
# SAME check keeps failing across multiple different scenes. That
# pattern means a structural/code bug, not random content variance —
# this is the automated version of manually eyeballing a failure log
# for repeats. Printed once, clearly, instead of scrolling past it
# scene by scene.
# ============================================================

import re
from collections import defaultdict

CHECK_NAMES = [
    "count_ok", "role_match_ok", "outfit_match_ok",
    "style_match_ok", "height_proportion_ok", "era_consistency_ok",
]

SYSTEMIC_THRESHOLD = 3   # same check failing on this many+ scenes = flag it


def extract_failing_checks(issue_text):
    if not issue_text:
        return []
    found = []
    for check in CHECK_NAMES:
        if check in issue_text:
            found.append(check)
    return found


def analyze(manifest, scene_keys):
    """manifest: full manifest dict. scene_keys: scenes from THIS run
    only (avoids flagging patterns from unrelated old stories)."""
    buckets = defaultdict(list)

    for key in scene_keys:
        entry = manifest.get(key, {})
        if entry.get("status") != "failed":
            continue
        error_text = entry.get("error", "")
        for check in extract_failing_checks(error_text):
            buckets[check].append(key)

    systemic = {check: keys for check, keys in buckets.items() if len(keys) >= SYSTEMIC_THRESHOLD}

    if not systemic:
        return

    print("\n" + "=" * 60)
    print("🔎 PATTERN ALERT — likely structural, not random variance")
    print("=" * 60)
    for check, keys in systemic.items():
        print(f"\n  [{check}] failed on {len(keys)} scenes: {', '.join(keys)}")
        print(f"  → Same check failing repeatedly usually means a code/prompt")
        print(f"    structure issue, not bad luck. Worth a manual look before")
        print(f"    retrying blindly — check if these scenes share something")
        print(f"    (character count, position layout, specific characters).")
        print(f"    Once diagnosed, add a permanent fix to known_fixes.py.")
    print("\n" + "=" * 60 + "\n")