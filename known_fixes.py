# ============================================================
# FILE: known_fixes.py
# PURPOSE: Registry of RECURRING, STRUCTURAL failure patterns with
# pre-approved fixes — checked BEFORE escalating to brain. This is
# how "never happen again" actually works: once a pattern is
# confirmed structural (not one-off variance), it goes here as a
# permanent, instant fix instead of being re-diagnosed from scratch
# by brain every single time it recurs.
#
# HOW TO ADD A NEW ENTRY:
# When pattern_watcher.py flags something as systemic (same check
# failing on 3+ scenes with a similar reason), read a few of those
# "issue" strings, find the common thread, and add a matcher below.
# ============================================================

KNOWN_FIXES = [
    # Example entry (currently unused — the swap bug it would have
    # caught is now fixed at the source in get_position_labels()).
    # Keep the shape for the next one:
    #
    # {
    #     "name": "center_right_position_swap",
    #     "match": lambda issue: "center" in issue.lower() and "right" in issue.lower()
    #                             and ("swap" in issue.lower() or "position" in issue.lower()),
    #     "extra_instruction": (
    #         "CRITICAL: the CENTER character must be rendered in the "
    #         "middle of the frame, and the RIGHT character on the right "
    #         "edge. Do not swap these two."
    #     ),
    # },
]


def check_known_fixes(issue_text):
    """Returns an extra_instruction string if a known pattern matches,
    else None. Checked before brain ever gets called."""
    if not issue_text:
        return None
    for entry in KNOWN_FIXES:
        try:
            if entry["match"](issue_text):
                return entry["extra_instruction"], entry["name"]
        except Exception:
            continue
    return None, None