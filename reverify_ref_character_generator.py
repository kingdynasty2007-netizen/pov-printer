# ============================================================
# FILE: reverify_ref_characters.py
# PURPOSE: Re-checks already-downloaded reference character images
# against the CURRENT verification criteria (including background
# and style-anchor checks that may not have existed when they were
# first generated). Same idea as reverify_existing.py, for
# characters instead of scenes.
#
# Imports generation/verification logic directly from
# ref_character_generator.py rather than duplicating it.
#
# On a catch: renames the file to {name}_fix.png and reports it —
# does not auto-regenerate. Small character counts don't need the
# 3-strikes escalation machinery reverify_existing.py has for scenes.
# RUN: python reverify_ref_characters.py
# ============================================================

import os
import json
import sys

# Settle which run this stage works on BEFORE importing ref_character_generator,
# which resolves its paths at import time. Bare -> newest run.
import run_paths
run_paths.bootstrap_stage(sys.argv)

from ref_character_generator import (
    REF_PROMPTS_FILE, REFERENCE_CHARACTERS_FILE, REF_OUTPUT_FOLDER,
    load_format_file, extract_image_style, local_image_to_data_uri,
    verify_reference_image,
)
from status_board import StatusBoard


def main():
    print("\n=== RE-VERIFY REFERENCE CHARACTERS ===\n")

    if not os.path.exists(REFERENCE_CHARACTERS_FILE):
        print(f"❌ {REFERENCE_CHARACTERS_FILE} not found — nothing to re-check")
        return

    with open(REFERENCE_CHARACTERS_FILE, "r", encoding="utf-8") as f:
        ref_characters = json.load(f)

    if not ref_characters:
        print("❌ reference_characters.json is empty")
        return

    style = None
    if os.path.exists(REF_PROMPTS_FILE):
        with open(REF_PROMPTS_FILE, "r", encoding="utf-8") as f:
            ref_data = json.load(f)
        style = ref_data.get("style") if "characters" in ref_data else None

    image_style_text = ""
    if style:
        format_content = load_format_file(style)
        if format_content:
            image_style_text = extract_image_style(format_content)

    names = list(ref_characters.keys())
    board = StatusBoard(names)
    board.start()

    anchor_data_uri = None
    caught = []
    clean = 0

    for i, name in enumerate(names):
        local_path = os.path.join(REF_OUTPUT_FOLDER, f"{name}.png")
        if not os.path.exists(local_path):
            board.update(name, "[SKIP] local file missing", done=True)
            continue

        board.update(name, "re-verifying")
        try:
            data_uri = local_image_to_data_uri(local_path)
        except Exception as e:
            board.update(name, f"[SKIP] could not read file: {e}", done=True)
            continue

        # First character re-checked has no anchor (nothing came before
        # it); it becomes the anchor for the rest of THIS audit pass.
        passed, issue = verify_reference_image(data_uri, name, image_style_text, anchor_data_uri=anchor_data_uri)

        if passed:
            board.update(name, "clean — no change", done=True)
            clean += 1
            if anchor_data_uri is None:
                anchor_data_uri = data_uri
            continue

        if passed is None:
            board.update(name, f"[SKIP] re-check unreachable: {issue}", done=True)
            continue

        base, ext = os.path.splitext(local_path)
        flagged_path = f"{base}_fix{ext}"
        try:
            if os.path.exists(flagged_path):
                os.remove(flagged_path)
            os.rename(local_path, flagged_path)
        except Exception:
            flagged_path = local_path

        board.update(name, f"[FLAGGED] {str(issue)[:60]}", done=True)
        caught.append((name, issue, flagged_path))

    board.stop()

    print(f"\n✅ Clean: {clean} / {len(names)}")
    print(f"🚩 Flagged: {len(caught)} / {len(names)}\n")

    if caught:
        print("These need manual regeneration (moved to *_fix.png, not auto-retried):")
        for name, issue, path in caught:
            print(f"  {name}: {issue}")
            print(f"    -> {path}")
        print("\nDelete the entry from reference_characters.json for any flagged")
        print("character, then run ref_character_generator.py again for just those.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")