# ============================================================
# FILE: ref_character_generator.py
# PURPOSE: Generates persistent reference images — now for THREE
# reference types, not just characters:
#   - character  (existing behavior, unchanged)
#   - location   (NEW — wide establishing shot, no people, reusable backdrop)
#   - prop       (NEW — isolated object, plain background, product-shot style)
#
# All three share ONE pipeline (generation, verification, retry,
# pacing, cooldown, upload) and — new — ONE style anchor: the very
# first reference generated (of any type) becomes the style anchor
# for every reference after it, so characters/locations/props all
# match the same art style, not just characters matching each other.
#
# ref_prompts.json now supports {"style":..., "characters": {...},
# "locations": {...}, "props": {...}}. Old flat {name: prompt} files
# (characters only, no "characters" key) still work unchanged.
#
# Backward compatibility: every name reverify_ref_character_generator.py
# imports (REF_PROMPTS_FILE, REFERENCE_CHARACTERS_FILE, REF_OUTPUT_FOLDER,
# load_format_file, extract_image_style, local_image_to_data_uri,
# verify_reference_image) is preserved with the same behavior when
# ref_type="character" (the default).
#
# ORIGINAL CHANGES (kept):
#   - Background-bleed fix + background_plain_ok check (characters/props).
#   - Parallelized: first reference processes alone (style anchor),
#     everyone else runs in parallel.
#   - Cross-reference style consistency via style_vs_anchor_ok.
#   - Lane-cycling is lock-protected for concurrent worker threads.
#   - JSON parsing now uses _clean_json_response() instead of
#     raw.strip("```json") — the old call stripped individual
#     characters, not the substring, and could corrupt valid JSON.
# ============================================================

import os
import re
import sys
import json
import time
import base64
import threading
import requests
from dotenv import load_dotenv

# Settle which run this stage works on BEFORE anything resolves paths.
# Bare  -> newest run.  `... RUN-0009` -> that run.  POV_LEGACY=1 -> root folders.
import run_paths
run_paths.bootstrap_stage(sys.argv)

from db import supabase
from status_board import StatusBoard

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_P = run_paths.get_paths()
REF_PROMPTS_FILE = _P["ref_prompts_file"]
REF_OUTPUT_FOLDER = _P["ref_images_dir"]
REFERENCE_CHARACTERS_FILE = _P["ref_characters_file"]
REFERENCE_LOCATIONS_FILE = _P["ref_locations_file"]
REFERENCE_PROPS_FILE = _P["ref_props_file"]
SCRIPTFORMAT_FOLDER = os.path.join(BASE_DIR, "scriptformat")

REF_TYPE_CHARACTER = "character"
REF_TYPE_LOCATION = "location"
REF_TYPE_PROP = "prop"

REF_TYPE_OUTPUT_FILE = {
    REF_TYPE_CHARACTER: REFERENCE_CHARACTERS_FILE,
    REF_TYPE_LOCATION: REFERENCE_LOCATIONS_FILE,
    REF_TYPE_PROP: REFERENCE_PROPS_FILE,
}

# ref_prompts.json key -> ref_type, processed in this order
REF_PROMPTS_KEY_TO_TYPE = [
    ("characters", REF_TYPE_CHARACTER),
    ("locations", REF_TYPE_LOCATION),
    ("props", REF_TYPE_PROP),
]

REF_TYPE_TABLE = {
    REF_TYPE_CHARACTER: "ref_characters",
    REF_TYPE_LOCATION: "ref_locations",
    REF_TYPE_PROP: "ref_props",
}

IMAGE_MODEL = "agnes-image-2.1-flash"
IMAGE_SIZE_BY_TYPE = {
    # Characters are full-body references, so the canvas is deliberately TALL
    # (2:3 instead of the old 3:4 768x1024). A 3:4 frame leaves the model
    # choosing between a closer shot and a full figure, and it kept choosing
    # closer — feet and ankles cropped off, which then failed the
    # full_body_ok check and burned retries. The extra vertical room lets a
    # head-to-toe figure fit with margin above the head and below the feet.
    REF_TYPE_CHARACTER: "1024x1536",  # 2:3 tall portrait, full body with margin
    REF_TYPE_LOCATION: "1024x576",    # matches the pipeline's 16:9 scene size
    REF_TYPE_PROP: "1024x1024",       # square product-style shot
}
# Tried only if the API rejects the size above (HTTP 400), so a provider that
# doesn't support 2:3 degrades to the old taller-than-square portrait instead of
# failing the whole stage. Characters: 3:4, then 9:16.
FALLBACK_SIZE_BY_TYPE = {
    REF_TYPE_CHARACTER: ["768x1024", "576x1024"],
}

# Framing escalates DETERMINISTICALLY per attempt instead of relying only on
# the verifier's free-text previous_issue, which the model largely ignored —
# in RUN-0015 john failed 3/3 with slightly different crop levels each time.
# Empty value = no extra text on that attempt.
FRAMING_ESCALATION = {
    REF_TYPE_CHARACTER: {
        2: "CRITICAL REFRAME — the previous attempt was cropped. Pull the "
           "camera much further back. Show the whole person from head to "
           "toe with a large amount of empty background on all sides. The "
           "body must occupy less than half the image height. Absolutely no "
           "medium shot, cowboy shot, knee-up shot or close-up.",
        3: "CRITICAL REFRAME — LAST ATTEMPT, the previous attempt was "
           "cropped again. Use a wide full-body shot with the figure small "
           "in a tall empty frame, standing far away from the camera. Both "
           "feet AND the ground beneath them must be visible.",
    },
}
IMAGE_URL = "https://apihub.agnes-ai.com/v1/images/generations"
CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
VERIFY_MODEL = "agnes-2.0-flash"

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

MAX_ATTEMPTS_PER_REFERENCE = 3
NETWORK_RETRY_ATTEMPTS = 3

SECONDS_BETWEEN_REQUESTS = 20
COOLDOWN_AFTER_LIMIT_SECONDS = 30

os.makedirs(REF_OUTPUT_FOLDER, exist_ok=True)


def _collect_keys(prefix):
    found = {}
    for name, value in os.environ.items():
        match = re.match(rf"^{prefix}(\d+)$", name)
        if match and value:
            found[int(match.group(1))] = value
    return found

def _labeled(prefix, tag):
    raw = _collect_keys(prefix)
    return {f"{tag}{n}": raw[n] for n in sorted(raw)}

GEN_KEYS = _labeled("AGNES_GEN_KEY_", "G")
VERIFY_KEYS = _labeled("AGNES_VERIFY_KEY_", "V")

if not GEN_KEYS:
    print("❌ No AGNES_GEN_KEY_N found in .env")
    sys.exit(1)
if not VERIFY_KEYS:
    print("❌ No AGNES_VERIFY_KEY_N found in .env")
    sys.exit(1)

_imgbb_key = os.environ.get("IMGBB_API_KEY")
if not _imgbb_key:
    print("❌ IMGBB_API_KEY not set in .env")
    sys.exit(1)

print(f"🔑 ref_character_generator: {len(GEN_KEYS)} gen key(s), {len(VERIFY_KEYS)} verify key(s)")

import itertools
_gen_cycle = itertools.cycle(GEN_KEYS.keys())
_verify_cycle = itertools.cycle(VERIFY_KEYS.keys())
_gen_cycle_lock = threading.Lock()
_verify_cycle_lock = threading.Lock()
_gen_last_time = {lane: 0 for lane in GEN_KEYS}
_verify_last_time = {lane: 0 for lane in VERIFY_KEYS}
_pace_lock = threading.Lock()

_cooldown_lock = threading.Lock()
_cooldown_until = 0

def _apply_cooldown():
    with _cooldown_lock:
        wait = _cooldown_until - time.time()
        if wait > 0:
            time.sleep(wait)

def _trigger_cooldown(seconds):
    global _cooldown_until
    with _cooldown_lock:
        candidate = time.time() + seconds
        if candidate > _cooldown_until:
            _cooldown_until = candidate

def _next_gen_lane():
    with _gen_cycle_lock:
        return next(_gen_cycle)

def _next_verify_lane():
    with _verify_cycle_lock:
        return next(_verify_cycle)

def _pace(lane, last_time_dict):
    _apply_cooldown()
    with _pace_lock:
        wait = SECONDS_BETWEEN_REQUESTS - (time.time() - last_time_dict.get(lane, 0))
        if wait > 0:
            time.sleep(wait)
        last_time_dict[lane] = time.time()


def load_format_file(style):
    filename = f"{style.lower().replace(' ', '_')}.txt"
    path = os.path.join(SCRIPTFORMAT_FOLDER, filename)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_image_style(format_content):
    lines = format_content.splitlines()
    capturing = False
    collected = []
    section_header_pattern = re.compile(r"^[A-Z][A-Z_]*:\s*$|#\s*(=+\s*)*$|#\s*\d+\.\s*[A-Z]")

    for line in lines:
        stripped = line.strip()
        if stripped == "IMAGE_STYLE:":
            capturing = True
            continue
        if capturing and section_header_pattern.match(stripped):
            break
        if capturing:
            collected.append(line)

    result = "\n".join(collected).strip()
    while result.startswith("#") and "=" in result:
        result = result.lstrip("#").lstrip().strip()
    return result


def _guess_mime(path_or_url):
    return "image/png" if path_or_url.lower().endswith(".png") else "image/jpeg"

def local_image_to_data_uri(path):
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{_guess_mime(path)};base64,{b64}"


def _clean_json_response(raw):
    """Strips a ```json ... ``` fence WITHOUT eating stray characters —
    the old raw.strip("```json") stripped individual characters, not
    the substring, and could silently corrupt valid JSON."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _local_path_for(name, ref_type):
    if ref_type == REF_TYPE_CHARACTER:
        return os.path.join(REF_OUTPUT_FOLDER, f"{name}.png")
    subfolder = os.path.join(REF_OUTPUT_FOLDER, ref_type + "s")
    os.makedirs(subfolder, exist_ok=True)
    return os.path.join(subfolder, f"{name}.png")


def _base_instruction_for_type(ref_type, image_style_text):
    style_instruction = ""
    if image_style_text:
        style_instruction = f"""

VISUAL RENDERING STYLE (apply ONLY the art/rendering technique described
below — line work, shading approach, coloring, illustration technique):
{image_style_text}"""

    if ref_type == REF_TYPE_LOCATION:
        override = """

CRITICAL OVERRIDE: Apply the rendering TECHNIQUE from the style above
(line work, shading, coloring) but this reference IS the location itself
— render its actual architecture/environment/setting in full. This is a
LOCATION reference sheet: a wide, empty establishing shot of the space
with NO people or characters in it, showing enough of the environment
that it can be reused as a consistent backdrop across multiple different
camera angles and scenes."""
        return (
            "IMPORTANT: Wide establishing shot, no people, no characters, "
            "nothing else occupying the frame."
            f"{style_instruction}{override}"
        )

    if ref_type == REF_TYPE_PROP:
        override = """

CRITICAL OVERRIDE: Ignore any mention of specific locations, buildings,
or environmental scenery in the style text above. This is a PROP
reference sheet, NOT a scene — it MUST have a plain, minimal,
undecorated background. No hands, no people, no other objects. The
prop is the only subject in frame."""
        return (
            "IMPORTANT: A single, isolated product-style reference shot of "
            "ONE object, centered, fully visible, not cropped, not held by "
            "anyone."
            f"{style_instruction}{override}"
        )

    # REF_TYPE_CHARACTER — original behavior, unchanged
    override = """

CRITICAL OVERRIDE: Ignore any mention of specific locations, buildings,
architecture, streets, objects, or environmental scenery in the style
text above. This is a character reference sheet, NOT a scene — it MUST
have a plain, minimal, undecorated background regardless of any setting
described above. Do not render buildings, furniture, landscape, or any
environmental detail."""
    return (
        "IMPORTANT: Full-body character reference, HEAD-TO-TOE, EXTREME LONG "
        "SHOT. The camera is far away and the figure is SMALL in the frame — "
        "it fills only the middle ~60% of the image height, with clearly "
        "empty space above the head and below the soles. Tall vertical "
        "orientation. Do NOT use a medium shot, cowboy shot, knee-up shot, "
        "waist-up shot, or close-up — those are exactly what was wrong with "
        "previous attempts. Front-facing, standing straight, arms relaxed at "
        "sides, neutral expression, hands empty, no props. Every part of the "
        "body is inside the frame: top of the head, shoulders, torso, hips, "
        "knees, ankles, and BOTH FEET. This is a CHARACTER REFERENCE — "
        "full-body framing and a plain background matter more than drama or "
        f"close-up detail.{style_instruction}{override}"
    )


def generate_ref_image(name, prompt, image_style_text, ref_type=REF_TYPE_CHARACTER, previous_issue=None, framing_note=""):
    base_instruction = _base_instruction_for_type(ref_type, image_style_text)

    if previous_issue:
        base_instruction += (
            f"\n\nCRITICAL — the previous attempt FAILED because: "
            f"\"{previous_issue}\". Correct this specifically."
        )

    if framing_note:
        base_instruction += f"\n\n{framing_note}"

    full_prompt = f"{prompt}\n\n{base_instruction}"
    size = IMAGE_SIZE_BY_TYPE.get(ref_type, IMAGE_SIZE_BY_TYPE[REF_TYPE_CHARACTER])

    # If the API rejects the preferred size outright (HTTP 400 mentioning the
    # size), fall back to the next one instead of failing all three attempts.
    # A hard "unsupported size" error will never fix itself by retrying.
    for size in [size] + [s for s in FALLBACK_SIZE_BY_TYPE.get(ref_type, []) if s != size]:
        payload = {"model": IMAGE_MODEL, "prompt": full_prompt, "size": size, "extra_body": {"response_format": "url"}}

        last_error = "unknown"
        size_rejected = False
        for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
            lane = _next_gen_lane()
            _pace(lane, _gen_last_time)
            headers = {"Authorization": f"Bearer {GEN_KEYS[lane]}", "Content-Type": "application/json"}

            try:
                response = requests.post(IMAGE_URL, headers=headers, json=payload, timeout=300)
            except requests.exceptions.RequestException as e:
                last_error = f"[{lane}] network error: {e}"
                continue

            if response.status_code in (429, 503):
                last_error = f"[{lane}] rate limited/queue full"
                _trigger_cooldown(COOLDOWN_AFTER_LIMIT_SECONDS)
                continue

            if not response.ok:
                body = response.text[:200]
                last_error = f"[{lane}] HTTP {response.status_code}: {body}"
                if response.status_code == 400 and "size" in body.lower():
                    size_rejected = True
                    break
                continue

            data = response.json()
            try:
                return data["data"][0]["url"]
            except (KeyError, IndexError):
                last_error = f"[{lane}] no url in response: {data}"
                continue

        if size_rejected:
            print(f"   ⚠️  Size {size} rejected by API ({last_error}) — trying next size")
            continue
        raise RuntimeError(last_error)

    raise RuntimeError(last_error)


def _checklist_for_type(ref_type, anchor_data_uri):
    if ref_type == REF_TYPE_LOCATION:
        lines = """1. no_people_ok: zero people or characters visible anywhere in frame?
2. single_location_ok: depicts ONE consistent location/setting, not a collage of multiple places?
3. wide_shot_ok: wide/establishing shot showing enough of the space to be reused as a backdrop from different angles?
4. style_match_ok: does the rendering TECHNIQUE match the required visual style below? Be strict."""
        keys = ["no_people_ok", "single_location_ok", "wide_shot_ok", "style_match_ok"]
    elif ref_type == REF_TYPE_PROP:
        lines = """1. single_object_ok: exactly ONE instance of the object, no duplicates?
2. no_hands_people_ok: not held by anyone, no hands or people visible?
3. full_object_ok: the entire object is visible, not cropped?
4. background_plain_ok: plain, minimal, undecorated background?
5. style_match_ok: does the rendering TECHNIQUE match the required visual style below? Be strict."""
        keys = ["single_object_ok", "no_hands_people_ok", "full_object_ok", "background_plain_ok", "style_match_ok"]
    else:
        lines = """1. person_count_ok: exactly ONE person visible, no extras?
2. front_facing_ok: facing forward, not a side profile or back view?
3. neutral_pose_ok: standing straight, arms relaxed, hands empty, no props?
4. full_body_ok: full body visible from head to feet, not cropped?
5. style_match_ok: does the rendering style match the required visual
   style above? Be strict — photorealistic FAILS if illustration was
   required, and vice versa. If no style was given, mark true by default.
6. background_plain_ok: is the background plain, minimal, and
   undecorated — NO buildings, furniture, landscape, or environmental
   scenery — even if the style text mentions a setting?"""
        keys = ["person_count_ok", "front_facing_ok", "neutral_pose_ok",
                "full_body_ok", "style_match_ok", "background_plain_ok"]

    if anchor_data_uri:
        lines += "\n7. style_vs_anchor_ok: does the candidate's rendering technique closely match the anchor image? Flag ANY noticeable stylistic difference."
        keys = keys + ["style_vs_anchor_ok"]

    return lines, keys


def verify_reference_image(image_url, name, image_style_text, ref_type=REF_TYPE_CHARACTER, anchor_data_uri=None):
    style_block = image_style_text if image_style_text else "No specific style requirement given — mark style_match_ok true by default."

    if anchor_data_uri:
        anchor_note = """
The FIRST image attached is a STYLE ANCHOR — the established illustration
style already used elsewhere in this same story (possibly a different
reference type). Ignore its specific identity/content — only compare
rendering technique (line weight, shading approach, color saturation,
overall illustration style). The FINAL image is the candidate to check.
"""
    else:
        anchor_note = "\nThe attached image is the candidate to check.\n"

    checklist_text, required_keys = _checklist_for_type(ref_type, anchor_data_uri)
    json_fields = ", ".join(f'"{k}": true/false' for k in required_keys)

    check_prompt = f"""
This is a {ref_type} reference sheet check for "{name}".
{anchor_note}
Required visual style for this image:
{style_block}

Check:
{checklist_text}

If ANY check above is false, describe in "issue" the SPECIFIC VISUAL
PROBLEM you actually see — not the name of the check. Bad: "full_body_ok".
Good: "feet and lower legs are cropped out of frame" or "the shading is
flat/cel-shaded rather than the soft gradients required". Be concrete
enough that someone who cannot see the image would know exactly what
is wrong.

Respond ONLY with JSON:
{{{json_fields},
"issue": "<specific visual description of the problem, or 'none'>"}}
"""
    content = [{"type": "text", "text": check_prompt}]
    if anchor_data_uri:
        content.append({"type": "image_url", "image_url": {"url": anchor_data_uri}})
    content.append({"type": "image_url", "image_url": {"url": image_url}})

    payload = {"model": VERIFY_MODEL, "messages": [{"role": "user", "content": content}]}

    last_error = "unknown"
    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        lane = _next_verify_lane()
        _pace(lane, _verify_last_time)
        headers = {"Authorization": f"Bearer {VERIFY_KEYS[lane]}", "Content-Type": "application/json"}

        try:
            response = requests.post(CHAT_URL, headers=headers, json=payload, timeout=120)
        except requests.exceptions.RequestException as e:
            last_error = f"[{lane}] network error: {e}"
            continue

        if response.status_code in (429, 503):
            last_error = f"[{lane}] rate limited/queue full"
            _trigger_cooldown(COOLDOWN_AFTER_LIMIT_SECONDS)
            continue

        if not response.ok:
            last_error = f"[{lane}] HTTP {response.status_code}: {response.text[:200]}"
            continue

        try:
            raw = response.json()["choices"][0]["message"]["content"]
            raw = _clean_json_response(raw)
            result = json.loads(raw)
            passed = all(result.get(k) is True for k in required_keys)
            return passed, result.get("issue", "unknown")
        except Exception as e:
            last_error = f"[{lane}] parse error: {e}"
            continue

    return None, last_error


def download_image(url, output_path):
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)


def upload_to_imgbb(image_path, name):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    response = requests.post(
        IMGBB_UPLOAD_URL,
        data={"key": _imgbb_key, "image": image_data, "name": f"ref_{name}"},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"ImgBB error {response.status_code}: {response.text[:200]}")
    data = response.json()
    try:
        return data["data"]["url"]
    except (KeyError, TypeError):
        raise RuntimeError(f"No URL in ImgBB response: {data}")


def save_ref_to_db(name, style, prompt, imgbb_url, local_path, ref_type=REF_TYPE_CHARACTER):
    table = REF_TYPE_TABLE.get(ref_type, "ref_characters")
    try:
        supabase.table(table).insert({
            "name": name, "style": style, "prompt": prompt,
            "imgbb_url": imgbb_url, "local_path": local_path,
        }).execute()
    except Exception as e:
        print(f"   ⚠️  Could not save to Supabase ({table}): {e}")


def write_reference_json(ref_map, ref_type):
    path = REF_TYPE_OUTPUT_FILE[ref_type]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ref_map, f, indent=2)
    print(f"✅ Written: {path}")

def write_reference_characters_json(ref_map):
    """Kept for backward compatibility — old name, characters only."""
    write_reference_json(ref_map, REF_TYPE_CHARACTER)


board = None


def process_one_reference(name, prompt, image_style_text, anchor_state, style, ref_type=REF_TYPE_CHARACTER):
    local_path = _local_path_for(name, ref_type)
    previous_issue = None

    for attempt in range(1, MAX_ATTEMPTS_PER_REFERENCE + 1):
        board.update(name, f"generating (attempt {attempt}/{MAX_ATTEMPTS_PER_REFERENCE})")
        framing_note = FRAMING_ESCALATION.get(ref_type, {}).get(attempt, "")
        try:
            agnes_url = generate_ref_image(name, prompt, image_style_text, ref_type=ref_type, previous_issue=previous_issue, framing_note=framing_note)
        except Exception as e:
            board.update(name, f"generation failed: {str(e)[:60]}")
            previous_issue = None
            continue

        board.update(name, "verifying")
        with anchor_state["lock"]:
            anchor_data_uri = anchor_state["data_uri"]
        passed, issue = verify_reference_image(agnes_url, name, image_style_text, ref_type=ref_type, anchor_data_uri=anchor_data_uri)

        if passed:
            board.update(name, "downloading")
            try:
                download_image(agnes_url, local_path)
            except Exception as e:
                board.update(name, f"download failed: {str(e)[:60]}")
                continue

            board.update(name, "uploading to ImgBB")
            try:
                imgbb_url = upload_to_imgbb(local_path, name)
            except Exception as e:
                board.update(name, f"ImgBB upload failed: {str(e)[:60]}")
                continue

            save_ref_to_db(name, style or "unknown", prompt, imgbb_url, local_path, ref_type=ref_type)
            board.update(name, "✅ ready", done=True)

            with anchor_state["lock"]:
                if anchor_state["data_uri"] is None:
                    anchor_state["data_uri"] = local_image_to_data_uri(local_path)

            return name, imgbb_url
        else:
            board.update(name, f"rejected: {str(issue)[:60]}")
            previous_issue = str(issue)

    board.update(name, "❌ FAILED after all attempts", done=True)
    return name, None

# Backward-compat alias — old code/imports calling this name still work.
process_one_character = process_one_reference


def main():
    global board

    print("\n=== REFERENCE GENERATOR (characters, locations, props) ===\n")

    if not os.path.exists(REF_PROMPTS_FILE):
        print(f"❌ {REF_PROMPTS_FILE} not found — run script_engine.py first")
        sys.exit(1)

    with open(REF_PROMPTS_FILE, "r", encoding="utf-8") as f:
        ref_data = json.load(f)

    if "characters" in ref_data or "locations" in ref_data or "props" in ref_data:
        style = ref_data.get("style")
    else:
        # Old flat {name: prompt} format — characters only, unchanged behavior.
        style = None
        ref_data = {"characters": ref_data}

    image_style_text = ""
    if style:
        format_content = load_format_file(style)
        if format_content:
            image_style_text = extract_image_style(format_content)
            if image_style_text:
                preview = image_style_text[:100] + ("..." if len(image_style_text) > 100 else "")
                print(f"🎨 Enforcing IMAGE_STYLE from '{style}': {preview}")
            else:
                print(f"⚠️  Format '{style}' has no IMAGE_STYLE section — style won't be enforced")
        else:
            print(f"⚠️  Could not load format file for style '{style}' — style won't be enforced")
    else:
        print("⚠️  No style recorded in ref_prompts.json — style won't be enforced")

    all_items = []  # (name, prompt, ref_type)
    for key, ref_type in REF_PROMPTS_KEY_TO_TYPE:
        group = ref_data.get(key) or {}
        if image_style_text:
            for name in group:
                group[name] = group[name].replace("[style-appropriate description]", image_style_text)
        for name, prompt in group.items():
            all_items.append((name, prompt, ref_type))

    if not all_items:
        print("❌ No characters, locations, or props found in ref_prompts.json — nothing to generate")
        sys.exit(1)

    by_type_count = {}
    for _, _, t in all_items:
        by_type_count[t] = by_type_count.get(t, 0) + 1
    summary = ", ".join(f"{n} {t}(s)" for t, n in by_type_count.items())
    print(f"📋 Found {len(all_items)} reference(s) — {summary}\n")

    board = StatusBoard([name for name, _, _ in all_items])
    board.start()

    # ONE shared style anchor across ALL types — the first reference
    # processed (regardless of type) establishes the art style; every
    # reference after it, characters/locations/props alike, is checked
    # against that same anchor.
    anchor_state = {"data_uri": None, "lock": threading.Lock()}
    successful_refs = {REF_TYPE_CHARACTER: {}, REF_TYPE_LOCATION: {}, REF_TYPE_PROP: {}}
    results_lock = threading.Lock()

    first_name, first_prompt, first_type = all_items[0]
    name, url = process_one_reference(first_name, first_prompt, image_style_text, anchor_state, style, ref_type=first_type)

    # HARD STOP if the anchor fails. This first item IS the style anchor —
    # every remaining item is verified against it. With no anchor, the rest
    # would burn 8 more generations and several minutes of quota to produce
    # references nobody can check for style consistency. Stop here instead,
    # and say plainly what went wrong so the prompt can be fixed.
    if not url:
        board.stop()
        print(f"\n❌ STYLE ANCHOR FAILED: '{first_name}' ({first_type}) "
              f"could not be generated after "
              f"{MAX_ATTEMPTS_PER_REFERENCE} attempts.")
        print("   Everything after it is verified against this image, so there is")
        print("   no point generating the rest — the pipeline is stopping here.")
        print("   Most common cause: the image is cropped (feet/legs cut off) or the")
        print("   style came back 3D/photorealistic instead of the required look.")
        print("   Edit that character's prompt in ref_prompts.json, then run again.")
        sys.exit(1)

    successful_refs[first_type][name] = url

    rest = all_items[1:]
    threads = []

    def worker(name, prompt, ref_type):
        result_name, result_url = process_one_reference(name, prompt, image_style_text, anchor_state, style, ref_type=ref_type)
        if result_url:
            with results_lock:
                successful_refs[ref_type][result_name] = result_url

    for name, prompt, ref_type in rest:
        t = threading.Thread(target=worker, args=(name, prompt, ref_type), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    board.stop()

    total_ok = sum(len(v) for v in successful_refs.values())
    if total_ok == 0:
        print("\n❌ No reference images were generated successfully")
        sys.exit(1)

    for ref_type, ref_map in successful_refs.items():
        if ref_map:
            write_reference_json(ref_map, ref_type)
        elif by_type_count.get(ref_type):
            print(f"⚠️  {by_type_count[ref_type]} {ref_type}(s) were requested but NONE succeeded — file not written")

    print(f"\n✅ Done — {total_ok}/{len(all_items)} reference(s) ready")
    print("   Next: batch_image_generator.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")