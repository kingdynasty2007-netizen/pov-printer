# ============================================================
# FILE: retry_refs.py
# PURPOSE: Retries ONLY the characters/locations/props that are
# missing from reference_characters.json / reference_locations.json
# / reference_props.json but ARE present in ref_prompts.json —
# i.e. everything ref_character_generator.py failed on last run.
#
# ref_character_generator.py itself has no resume logic — a crash
# on item 6 of 8 means re-running it burns time/quota regenerating
# items 1-5 again. This fixes that without touching that file.
#
# Reuses generation/verification logic directly from
# ref_character_generator.py (does not duplicate it).
#
# EXIT CODE IS THE PIPELINE GATE:
#   0 = every character in ref_prompts.json now exists in
#       reference_characters.json
#   1 = one or more characters are STILL missing
# ref_character_generator.py only exits non-zero when ALL types
# fail, so a partial failure (some locations succeeded, every
# character failed) exits 0 there and would let the pipeline walk
# into image_core.py, which sys.exit(1)s at import when
# reference_characters.json is missing/empty. This script exists
# to make that failure visible as a non-zero exit.
#
# Missing LOCATIONS or PROPS do NOT fail the run — they are
# optional enrichments (see image_core._load_optional_reference_json).
#
# RUN: python retry_refs.py
# RUN (just one type): python retry_refs.py --type location
# ============================================================

import os
import sys
import json
import argparse
import threading

import run_paths
run_paths.bootstrap_stage(sys.argv)

from ref_character_generator import (
    REF_PROMPTS_FILE, REF_TYPE_OUTPUT_FILE, REF_PROMPTS_KEY_TO_TYPE,
    REF_TYPE_CHARACTER, REF_TYPE_LOCATION, REF_TYPE_PROP,
    load_format_file, extract_image_style,
    local_image_to_data_uri, process_one_reference,
    write_reference_json,
)
import ref_character_generator as refgen
from status_board import StatusBoard


def load_existing(ref_type):
    path = REF_TYPE_OUTPUT_FILE[ref_type]
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else {}


def find_anchor_local_path():
    """Try to find a local file for ANY already-successful reference
    (any type) to use as the style anchor, so retries stay visually
    consistent with what already succeeded."""
    for _, t in REF_PROMPTS_KEY_TO_TYPE:
        existing = load_existing(t)
        if not existing:
            continue
        name = next(iter(existing))
        if t == REF_TYPE_CHARACTER:
            local_path = os.path.join(refgen.REF_OUTPUT_FOLDER, f"{name}.png")
        else:
            local_path = os.path.join(refgen.REF_OUTPUT_FOLDER, t + "s", f"{name}.png")
        if os.path.exists(local_path):
            return local_path
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["character", "location", "prop"], default=None,
                         help="Only retry this reference type. Default: all types.")
    parser.add_argument("run_id", nargs="?", default=None,
                        help="Optional RUN-XXXX argument. Already consumed by "
                             "run_paths.bootstrap_stage() at import time; declared "
                             "here only so argparse does not reject it as unknown.")
    args = parser.parse_args()

    print("\n=== RETRY REFERENCES (characters / locations / props) ===\n")

    if not os.path.exists(REF_PROMPTS_FILE):
        print(f"❌ {REF_PROMPTS_FILE} not found — nothing to retry")
        sys.exit(1)

    with open(REF_PROMPTS_FILE, "r", encoding="utf-8") as f:
        ref_data = json.load(f)

    if "characters" in ref_data or "locations" in ref_data or "props" in ref_data:
        style = ref_data.get("style")
    else:
        style = None
        ref_data = {"characters": ref_data}

    image_style_text = ""
    if style:
        format_content = load_format_file(style)
        if format_content:
            image_style_text = extract_image_style(format_content)

    # Figure out what's missing per type.
    missing = []  # (name, prompt, ref_type)
    for key, ref_type in REF_PROMPTS_KEY_TO_TYPE:
        if args.type and ref_type != args.type:
            continue
        wanted = ref_data.get(key) or {}
        already_have = load_existing(ref_type)
        for name, prompt in wanted.items():
            if name not in already_have:
                if image_style_text:
                    prompt = prompt.replace("[style-appropriate description]", image_style_text)
                missing.append((name, prompt, ref_type))

    if not missing:
        print("✅ Nothing to retry — every expected reference already exists.")
        _exit_with_character_gate(ref_data)
        return

    by_type_count = {}
    for _, _, t in missing:
        by_type_count[t] = by_type_count.get(t, 0) + 1
    summary = ", ".join(f"{n} {t}(s)" for t, n in by_type_count.items())
    print(f"📋 {len(missing)} reference(s) missing — {summary}\n")

    # Seed the style anchor from an ALREADY-successful reference so
    # retries match what already generated fine, instead of drifting.
    anchor_state = {"data_uri": None, "lock": threading.Lock()}
    anchor_source = find_anchor_local_path()
    if anchor_source:
        anchor_state["data_uri"] = local_image_to_data_uri(anchor_source)
        print(f"🎨 Using existing reference as style anchor: {anchor_source}")
    else:
        print("⚠️  No existing successful reference found to anchor style — "
              "the first retry item will be processed alone to establish the anchor.")

    board = StatusBoard([name for name, _, _ in missing])
    board.start()

    # process_one_reference() reports progress through the MODULE-LEVEL board
    # in ref_character_generator, which is only ever assigned by ITS main().
    # This script builds its own local board, so without this line every
    # worker thread dies on the first board.update() call with
    # 'NoneType' object has no attribute 'update' — before making a single
    # API request, which then honestly reports "0 succeeded".
    refgen.board = board

    successful = {REF_TYPE_CHARACTER: {}, REF_TYPE_LOCATION: {}, REF_TYPE_PROP: {}}
    results_lock = threading.Lock()
    threads = []

    def record(result_name, result_url, ref_type):
        if result_url:
            with results_lock:
                successful[ref_type][result_name] = result_url

    def worker(name, prompt, ref_type):
        # A thread dying here used to be invisible: it vanished, recorded
        # nothing, and the summary just said that item "still failed".
        try:
            result_name, result_url = process_one_reference(
                name, prompt, image_style_text, anchor_state, style, ref_type=ref_type
            )
        except Exception as e:
            print(f"❌ {name}: crashed — {str(e)[:160]}")
            return
        record(result_name, result_url, ref_type)

    if anchor_state["data_uri"] is not None:
        # Anchor already known — every item can run in parallel.
        for name, prompt, ref_type in missing:
            t = threading.Thread(target=worker, args=(name, prompt, ref_type), daemon=True)
            threads.append(t)
            t.start()
    else:
        # No anchor yet. Running all of them in parallel would let several
        # threads race to set anchor_state["data_uri"], and whichever wins
        # leaves the others verified against nothing — exactly the drift
        # ref_character_generator.main() avoids by processing the first
        # item alone. Mirror that here.
        first_name, first_prompt, first_type = missing[0]
        print(f"🎨 Establishing style anchor from: {first_name}")
        try:
            result_name, result_url = process_one_reference(
                first_name, first_prompt, image_style_text, anchor_state, style, ref_type=first_type
            )
        except Exception as e:
            print(f"❌ {first_name}: crashed — {str(e)[:160]}")
            result_name, result_url = first_name, None
        record(result_name, result_url, first_type)
        rest = missing[1:]
        for name, prompt, ref_type in rest:
            t = threading.Thread(target=worker, args=(name, prompt, ref_type), daemon=True)
            threads.append(t)
            t.start()

    for t in threads:
        t.join()

    board.stop()

    total_ok = sum(len(v) for v in successful.values())

    for ref_type, ref_map in successful.items():
        if not ref_map:
            continue
        merged = load_existing(ref_type)
        merged.update(ref_map)
        write_reference_json(merged, ref_type)

    still_missing = len(missing) - total_ok
    print(f"\n✅ Retried {len(missing)}, {total_ok} succeeded, {still_missing} still failed")
    if still_missing:
        print("   Run python retry_refs.py again for the rest.")

    _exit_with_character_gate(ref_data)


def _exit_with_character_gate(ref_data):
    """
    The actual gate. A missing CHARACTER is fatal for the run
    (image_core.py hard-stops at import without reference_characters.json);
    a missing location/prop is not. Re-reads the JSON files rather than
    trusting the in-memory results, so a failed write is caught too.
    """
    wanted_chars = set((ref_data.get("characters") or {}).keys())
    have_chars = set(load_existing(REF_TYPE_CHARACTER).keys())
    absent = sorted(wanted_chars - have_chars)

    if not wanted_chars:
        # No characters requested at all — nothing to gate on.
        return

    if absent:
        print(f"\n❌ {len(absent)} character(s) still missing: {', '.join(absent)}")
        print("   reference_characters.json is incomplete — the image stage would "
              "hard-stop at import. Pipeline cannot continue.")
        sys.exit(1)

    print(f"✅ All {len(wanted_chars)} character reference(s) present.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")
