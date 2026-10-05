# ============================================================
# FILE: image_core.py
# CHANGE: added a GLOBAL cooldown for generation, but only
# REACTIVE — it does nothing during a healthy run. It only
# activates the moment Agnes returns a 503 ("queue is full"),
# at which point EVERY gen lane backs off together for a real
# amount of time before anyone retries — instead of every lane
# instantly walking back into the same full queue, which is what
# caused the endless retry loop at 85-scene scale.
#
# CHANGE 2: verify_image now ALSO scores generated characters
# against their reference image for visual identity similarity
# (face/hair/build), 0-100, and requires >= MIN_IDENTITY_SIMILARITY_SCORE.
# This runs alongside the existing checklist, not instead of it.
#
# CHANGE 3: fixed a JSON-parsing bug — raw.strip("```json") was
# stripping individual characters, not the substring, which could
# silently corrupt valid model output. Replaced with _clean_json_response().
#
# CHANGE 4: scenes can now optionally reference a LOCATION and/or
# PROPS (in addition to CHARACTERS) — persistent references just
# like characters, but for backgrounds/settings and recurring
# objects. Optional: a scene with no LOCATION:/PROPS: line behaves
# exactly as before.
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

load_dotenv()

import run_paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SECONDS_BETWEEN_REQUESTS = 20
SECONDS_BETWEEN_VERIFY = 20
MAX_VERIFY_CALL_ATTEMPTS = 3
MAX_BLIND_RETRIES = 3
MAX_BRAIN_RETRIES = 3

REFERENCE_CACHE_RETRY_ATTEMPTS = 3
REFERENCE_CACHE_RETRY_DELAY_SECONDS = 3

GEN_COOLDOWN_ON_QUEUE_FULL_SECONDS = 45   # how long ALL gen lanes back off after a 503

MIN_IDENTITY_SIMILARITY_SCORE = 80   # generated character must score >= this vs reference

IMAGE_MODEL = "agnes-image-2.1-flash"
VERIFY_MODEL = "agnes-2.0-flash"
IMAGE_SIZE = "1024x576"

_P = run_paths.get_paths()
SCENES_FILE = _P["scenes_file"]
FRAMES_FILE = _P["frames_file"]
OUTPUT_FOLDER = _P["images_dir"]
MANIFEST_FILE = _P["manifest_file"]
REFERENCE_CHARACTERS_FILE = _P["ref_characters_file"]
REFERENCE_LOCATIONS_FILE = _P["ref_locations_file"]
REFERENCE_PROPS_FILE = _P["ref_props_file"]

CROWD_CHARACTERS = set()

IMAGE_URL = "https://apihub.agnes-ai.com/v1/images/generations"
CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def _load_reference_images():
    if os.path.exists(REFERENCE_CHARACTERS_FILE):
        with open(REFERENCE_CHARACTERS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                if data:
                    return data
    print(f"❌ {REFERENCE_CHARACTERS_FILE} not found or empty.")
    print(f"   Run ref_character_generator.py first.")
    sys.exit(1)

REFERENCE_IMAGES = _load_reference_images()


def _load_optional_reference_json(path, label):
    """
    Like _load_reference_images but OPTIONAL — locations/props aren't
    required for every story. Returns {} (with a note, not an error)
    if the file is missing or empty.
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                data = json.loads(content)
                if data:
                    return data
    print(f"ℹ️  No {label} references found ({os.path.basename(path)}) — scenes with no matching LOCATION/PROPS tag are unaffected.")
    return {}

REFERENCE_LOCATIONS = _load_optional_reference_json(REFERENCE_LOCATIONS_FILE, "location")
REFERENCE_PROPS = _load_optional_reference_json(REFERENCE_PROPS_FILE, "prop")


def _guess_mime(path_or_url):
    lower = path_or_url.lower()
    if lower.endswith(".png"):
        return "image/png"
    return "image/jpeg"

def _fetch_as_data_uri(url):
    last_error = None
    for attempt in range(1, REFERENCE_CACHE_RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            b64 = base64.b64encode(response.content).decode("utf-8")
            return f"data:{_guess_mime(url)};base64,{b64}"
        except Exception as e:
            last_error = e
            if attempt < REFERENCE_CACHE_RETRY_ATTEMPTS:
                time.sleep(REFERENCE_CACHE_RETRY_DELAY_SECONDS)
    raise last_error

def local_image_to_data_uri(path):
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{_guess_mime(path)};base64,{b64}"

REFERENCE_IMAGES_B64 = {}
_reference_cache_failures = []
print("📥 Caching reference images as base64...")
for _name, _url in REFERENCE_IMAGES.items():
    try:
        REFERENCE_IMAGES_B64[_name] = _fetch_as_data_uri(_url)
        print(f"   ✓ {_name}")
    except Exception as e:
        print(f"   ❌ {_name}: could not cache after {REFERENCE_CACHE_RETRY_ATTEMPTS} attempt(s) ({e})")
        REFERENCE_IMAGES_B64[_name] = None
        _reference_cache_failures.append(_name)

if _reference_cache_failures:
    print(f"\n❌ HARD STOP: {len(_reference_cache_failures)} reference image(s) failed to cache: "
          f"{', '.join(_reference_cache_failures)}")
    sys.exit(1)


def _cache_optional_reference_set(ref_dict, label):
    """Same caching as characters, but a failure here just drops that
    one entry (with a warning) instead of hard-stopping the whole run
    — locations/props are optional enrichments, not required inputs."""
    cache = {}
    for name, url in ref_dict.items():
        try:
            cache[name] = _fetch_as_data_uri(url)
            print(f"   ✓ {label}:{name}")
        except Exception as e:
            print(f"   ⚠️  {label}:{name} could not be cached ({e}) — scenes tagging it will skip this reference")
    return cache

if REFERENCE_LOCATIONS:
    print("📥 Caching location references as base64...")
    REFERENCE_LOCATIONS_B64 = _cache_optional_reference_set(REFERENCE_LOCATIONS, "location")
else:
    REFERENCE_LOCATIONS_B64 = {}

if REFERENCE_PROPS:
    print("📥 Caching prop references as base64...")
    REFERENCE_PROPS_B64 = _cache_optional_reference_set(REFERENCE_PROPS, "prop")
else:
    REFERENCE_PROPS_B64 = {}


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
    print("❌ No generation keys found. Add AGNES_GEN_KEY_1=... to your .env file")
    sys.exit(1)
if not VERIFY_KEYS:
    print("❌ No verification keys found. Add AGNES_VERIFY_KEY_1=... to your .env file")
    sys.exit(1)

GEN_HEADERS = {lane: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} for lane, key in GEN_KEYS.items()}
VERIFY_HEADERS = {lane: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} for lane, key in VERIFY_KEYS.items()}

print(f"🔑 Loaded {len(GEN_KEYS)} generation key(s): {', '.join(GEN_KEYS)}")
print(f"🔑 Loaded {len(VERIFY_KEYS)} verification key(s): {', '.join(VERIFY_KEYS)}")


def classify_error(error_text):
    text = (error_text or "").lower()
    content_policy_signals = ["content_policy", "content policy", "moderation", "flagged", "violates", "not allowed"]
    transient_signals = ["network error", "timeout", "connection reset", "connection aborted",
                          "http 500", "http 502", "http 503", "http 504", "upstream_error",
                          "temporarily unavailable", "econnreset", "read timed out", "rate limit", "429",
                          "queue is full"]
    if any(s in text for s in content_policy_signals):
        return "content_policy"
    if any(s in text for s in transient_signals):
        return "transient"
    return "other"


_manifest_lock = threading.Lock()

def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    return {}

def update_manifest_entry(scene_key, updates):
    with _manifest_lock:
        manifest = load_manifest()
        manifest.setdefault(scene_key, {}).update(updates)
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)


def parse_scene(raw_block, block_position):
    lines = raw_block.strip().splitlines()
    character_names = list(REFERENCE_IMAGES.keys())[:1] or ["main_character"]
    location_name = None
    prop_names = []
    remaining_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("CHARACTERS:"):
            character_names = [n.strip() for n in stripped.split(":", 1)[1].split(",") if n.strip()]
        elif stripped.upper().startswith("LOCATION:"):
            value = stripped.split(":", 1)[1].strip().lower()
            location_name = value or None
        elif stripped.upper().startswith("PROPS:"):
            prop_names = [n.strip().lower() for n in stripped.split(":", 1)[1].split(",") if n.strip()]
        else:
            remaining_lines.append(line)

    full_text = "\n".join(remaining_lines).strip()

    frame_id = None
    frame_match = re.search(r"FRAME\s+0*?(\d+)\s*:\s*(.*)", full_text, re.IGNORECASE | re.DOTALL)
    if frame_match:
        frame_id = int(frame_match.group(1))
        scene_text = frame_match.group(2).strip()
    else:
        scene_text = full_text

    character_names = [n.lower() for n in character_names]
    unknown = [n for n in character_names if n not in REFERENCE_IMAGES and n not in CROWD_CHARACTERS]
    if unknown:
        print(f"❌ Scene block #{block_position}: unknown character name(s) {unknown}")
        print(f"   Known individuals: {list(REFERENCE_IMAGES.keys())}")
        print(f"   Known crowd labels: {list(CROWD_CHARACTERS)}")
        print(f"   Fix the CHARACTERS: line. Stopping — not guessing.")
        sys.exit(1)

    # LOCATION/PROPS are soft references — unlike characters, an
    # unregistered name doesn't stop the run. It just means this scene
    # gets no visual reference for that tag (the scene's own prose
    # still describes it; it just won't be pinned to a fixed look).
    if location_name and location_name not in REFERENCE_LOCATIONS_B64:
        print(f"ℹ️  Scene block #{block_position}: LOCATION '{location_name}' has no cached reference — using text description only.")
        location_name = None

    prop_names = [p for p in prop_names if p in REFERENCE_PROPS_B64]

    return {
        "characters": character_names,
        "text": scene_text,
        "frame_id": frame_id,
        "location": location_name,
        "props": prop_names,
    }


def load_scenes(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    raw_blocks = [b.strip() for b in content.split("---") if b.strip()]
    if not raw_blocks:
        print(f"❌ No scenes found in {file_path}")
        sys.exit(1)

    scenes = []
    for position, raw_block in enumerate(raw_blocks, start=1):
        parsed = parse_scene(raw_block, position)
        scene_number = parsed["frame_id"] if parsed["frame_id"] is not None else position
        scenes.append((scene_number, parsed["characters"], parsed["text"], parsed["location"], parsed["props"]))

    return scenes


def get_current_scene_keys():
    scenes = load_scenes(SCENES_FILE)
    return {f"scene_{number:03d}" for number, _, _, _, _ in scenes}


def get_position_labels(named_count):
    if named_count <= 1:
        return []
    elif named_count == 2:
        return ["LEFT", "RIGHT"]
    elif named_count == 3:
        return ["LEFT", "CENTER", "RIGHT"]
    else:
        return ["FAR LEFT", "LEFT", "CENTER", "RIGHT", "FAR RIGHT"][:named_count]


STYLE_PROMPT = """
VISUAL STYLE:
Match the reference image's rendering style exactly.

Generate exactly the number of people described below — no
duplicate or repeated figures. Preserve each character's body
proportions and build exactly as shown in their reference image —
do not make any character notably taller, shorter, heavier, or
thinner than their reference. Do NOT stretch, elongate, or
unnaturally extend any character's limbs, neck, torso, or overall
body compared to their reference proportions — this includes subtle
stretching, not just obvious distortion.
"""

POSE_INDEPENDENCE_NOTE = """
POSE AND ACTION — IMPORTANT:
The reference image defines this character's IDENTITY ONLY: face,
hair, body proportions, and clothing DESIGN. It does NOT define their
pose, hand position, gesture, or stance in this new scene. Do not
copy the reference image's pose. The character's physical pose, hand
placement, and action must match ONLY the scene description below —
even if that means a completely different pose than shown in the
reference.
"""


def build_prompt(characters, scene_text, extra_instruction="", location=None, props=None):
    props = props or []
    named = [c for c in characters if c in REFERENCE_IMAGES]
    crowd = [c for c in characters if c in CROWD_CHARACTERS]
    positions = get_position_labels(len(named))

    if not named:
        instruction = (
            "Depict this scene in the established visual style. "
            "Any people shown should be dressed and styled consistently "
            "with the setting used throughout this story."
        )
    elif len(named) == 1 and not crowd:
        instruction = (
            "Use the provided reference image as the authoritative "
            "reference for character design, proportions, and style. "
            "Create a NEW scene based on the description below."
        )
    else:
        lines = ["REFERENCE IMAGES:"]
        for i, name in enumerate(named, start=1):
            display_name = name.replace("_", " ").upper()
            position_label = positions[i - 1] if i <= len(positions) else "BACKGROUND"
            lines.append(
                f"- Image {i} = {display_name}, positioned {position_label}. "
                f"{display_name} keeps their own clothing, hair, and role at all times. "
                f"Never apply this character's clothing, hair, or role to any other person."
            )
        lines.append(
            "Each named character above draws ONLY from their own labeled "
            "reference image. Do not blend, swap, or share traits between them."
        )
        if crowd:
            crowd_names = ", ".join(c.replace("_", " ") for c in crowd)
            lines.append(
                f"Additional group/background characters ({crowd_names}) have no "
                f"fixed reference — depict them in era-appropriate style only."
            )
        instruction = "\n".join(lines)

    parts = [instruction, STYLE_PROMPT]
    if named:
        parts.append(POSE_INDEPENDENCE_NOTE)

    if location and location in REFERENCE_LOCATIONS:
        parts.append(
            "LOCATION REFERENCE:\nAn additional reference image is provided for this "
            f"scene's setting: {location.replace('_', ' ').upper()}. Match its "
            "architecture, terrain, and lighting exactly — characters and action "
            "happen WITHIN this established location; do not redesign the location itself."
        )

    if props:
        known_props = [p for p in props if p in REFERENCE_PROPS]
        if known_props:
            prop_list = ", ".join(p.replace("_", " ").upper() for p in known_props)
            parts.append(
                f"PROP REFERENCE(S):\nAdditional reference image(s) are provided for: "
                f"{prop_list}. Render each exactly as shown in its reference when it "
                "appears in this scene — same shape, material, and color."
            )

    if extra_instruction:
        parts.append("ADDITIONAL CONSTRAINT (important):\n" + extra_instruction)
    parts.append("SCENE:\n" + scene_text)
    return "\n".join(parts)


_gen_last_time = {lane: 0 for lane in GEN_KEYS}
_gen_locks = {lane: threading.Lock() for lane in GEN_KEYS}
_verify_last_time = {lane: 0 for lane in VERIFY_KEYS}
_verify_locks = {lane: threading.Lock() for lane in VERIFY_KEYS}

def rate_limit_gen(lane):
    with _gen_locks[lane]:
        wait = SECONDS_BETWEEN_REQUESTS - (time.time() - _gen_last_time[lane])
        if wait > 0:
            time.sleep(wait)
        _gen_last_time[lane] = time.time()

def rate_limit_verify(lane):
    with _verify_locks[lane]:
        wait = SECONDS_BETWEEN_VERIFY - (time.time() - _verify_last_time[lane])
        if wait > 0:
            time.sleep(wait)
        _verify_last_time[lane] = time.time()


# ---- REACTIVE global cooldown for generation — does NOTHING during a
# healthy run. Only activates once Agnes actually reports its queue is
# full, at which point every gen lane waits together before retrying. ----
_gen_cooldown_lock = threading.Lock()
_gen_cooldown_until = 0

def _apply_gen_cooldown():
    with _gen_cooldown_lock:
        wait = _gen_cooldown_until - time.time()
        if wait > 0:
            time.sleep(wait)

def _trigger_gen_cooldown(seconds):
    global _gen_cooldown_until
    with _gen_cooldown_lock:
        candidate = time.time() + seconds
        if candidate > _gen_cooldown_until:
            _gen_cooldown_until = candidate


def generate_image_url(lane, characters, scene_text, extra_instruction="", location=None, props=None):
    props = props or []
    prompt = build_prompt(characters, scene_text, extra_instruction, location=location, props=props)

    reference_urls = [REFERENCE_IMAGES[name] for name in characters if name in REFERENCE_IMAGES]
    if location and location in REFERENCE_LOCATIONS:
        reference_urls.append(REFERENCE_LOCATIONS[location])
    for prop in props:
        if prop in REFERENCE_PROPS:
            reference_urls.append(REFERENCE_PROPS[prop])

    payload = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": IMAGE_SIZE,
        "extra_body": {"image": reference_urls, "response_format": "url"},
    }

    _apply_gen_cooldown()
    rate_limit_gen(lane)

    try:
        response = requests.post(IMAGE_URL, headers=GEN_HEADERS[lane], json=payload, timeout=300)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"network error: {e}")

    if response.status_code == 503:
        _trigger_gen_cooldown(GEN_COOLDOWN_ON_QUEUE_FULL_SECONDS)
        raise RuntimeError(f"http 503 (queue full — all lanes cooling down {GEN_COOLDOWN_ON_QUEUE_FULL_SECONDS}s): {response.text[:300]}")

    if not response.ok:
        raise RuntimeError(f"http {response.status_code}: {response.text[:300]}")

    data = response.json()
    try:
        return data["data"][0]["url"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"no url returned: {data}")


def download_image(url, output_path):
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)


def _clean_json_response(raw):
    """Strips a ```json ... ``` fence WITHOUT eating stray characters —
    the old raw.strip("```json") stripped individual characters, not
    the substring, and could silently corrupt valid JSON."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def verify_image(lane, image_url, characters, scene_text, location=None, props=None):
    props = props or []
    character_list = ", ".join(c.replace("_", " ") for c in characters)
    named = [c for c in characters if c in REFERENCE_IMAGES]
    ref_data_uris = [REFERENCE_IMAGES_B64.get(c) for c in named]
    have_refs = bool(named) and all(ref_data_uris)
    positions = get_position_labels(len(named))

    known_location = location if (location and location in REFERENCE_LOCATIONS_B64) else None
    known_props = [p for p in props if p in REFERENCE_PROPS_B64]

    ref_labels = "\n".join(
        f"- Reference image {i + 1} = {c.replace('_', ' ').upper()}"
        for i, c in enumerate(named)
    )

    identity_check = ""
    if len(named) >= 2 and positions:
        position_map = "\n".join(
            f"- {positions[i]} should be {named[i].replace('_',' ').upper()}"
            for i in range(len(named))
        )
        identity_check = f"""
Before scoring, explicitly check each position:
{position_map}
For EACH position, compare hair, build, and clothing color against
that character's reference image. If ANY position shows a different
character than assigned above, set role_match_ok to false and name
the swap explicitly in "issue".
"""

    non_reference = [c for c in characters if c not in named]
    crowd_note = ""
    if non_reference:
        crowd_note = (
            f"\n{', '.join(c.replace('_',' ') for c in non_reference)} have no "
            f"reference image and are not one consistent person — do not apply "
            f"outfit_match_ok or height_proportion_ok scrutiny to them."
        )

    # ---- Build the ordered list of reference images attached to this
    # verify call, and remember which index range belongs to what, so
    # the prompt text and the actual attached images always agree. ----
    ref_images_in_order = list(ref_data_uris) if have_refs else []
    next_ref_number = len(ref_images_in_order) + 1

    location_label_line = ""
    if known_location:
        location_label_line = f"- Reference image {next_ref_number} = LOCATION: {known_location.replace('_',' ').upper()} (the required setting/background)"
        ref_images_in_order.append(REFERENCE_LOCATIONS_B64[known_location])
        next_ref_number += 1

    props_label_lines = []
    for p in known_props:
        props_label_lines.append(f"- Reference image {next_ref_number} = PROP: {p.replace('_',' ').upper()}")
        ref_images_in_order.append(REFERENCE_PROPS_B64[p])
        next_ref_number += 1

    extra_ref_labels = "\n".join([l for l in [location_label_line] + props_label_lines if l])

    if have_refs or known_location or known_props:
        intro = f"""You are given reference image(s) followed by ONE generated scene
image (the LAST image). Compare directly against the reference(s) —
do not guess from the text alone.

{ref_labels}
{extra_ref_labels}
- The final image is the GENERATED SCENE to check.{crowd_note}
{identity_check}"""
    else:
        intro = "Look at this generated scene image and check it against the description below."

    similarity_check_text = ""
    similarity_json_field = ""
    if have_refs:
        similarity_check_text = """
8. identity_similarity_score: score 0-100, how closely does each named
   character's FACE, HAIR, and BUILD match their reference image —
   ignore pose and outfit color (already covered above). 100 = clearly
   the same person, 0 = no resemblance. If multiple named characters,
   give the LOWEST individual score (the worst match), not an average.
"""
        similarity_json_field = ',\n"identity_similarity_score": <integer 0-100>'

    background_check_text = ""
    background_json_field = ""
    if known_location:
        background_check_text = """
9. background_match_ok: does the scene's background/setting match the
   LOCATION reference image — same architecture, terrain, and general
   layout (lighting/time-of-day may vary with the scene text)?
"""
        background_json_field = ',\n"background_match_ok": true/false'

    props_check_text = ""
    props_json_field = ""
    if known_props:
        props_check_text = """
10. props_match_ok: does/do the PROP reference object(s) appear in the
    scene (if the scene text calls for them) and match their reference
    image(s) — same shape, material, and color?
"""
        props_json_field = ',\n"props_match_ok": true/false'

    check_prompt = f"""
{intro}

Expected characters: {character_list}
Scene description: {scene_text}

Be STRICT. If unsure, mark false rather than assuming it's fine.

Check ALL of the following:
1. count_ok: exact number of named foreground characters present?
2. role_match_ok: correct position AND correct action per character?
3. outfit_match_ok: clothing matches each referenced character exactly?
4. style_match_ok: rendering style identical to the reference(s)?
5. height_proportion_ok: height/limb/neck/torso match reference exactly —
   watch specifically for stretching or elongation?
6. era_consistency_ok: no modern/anachronistic elements anywhere?
7. pose_action_ok: pose matches the SCENE TEXT, not the reference image's pose?
{similarity_check_text}{background_check_text}{props_check_text}
Respond ONLY with JSON, no other text:
{{"count_ok": true/false, "role_match_ok": true/false,
"outfit_match_ok": true/false, "style_match_ok": true/false,
"height_proportion_ok": true/false, "era_consistency_ok": true/false,
"pose_action_ok": true/false{similarity_json_field}{background_json_field}{props_json_field},
"issue": "<name the failing check(s) and describe briefly, or 'none'>"}}
"""

    content = [{"type": "text", "text": check_prompt}]
    for data_uri in ref_images_in_order:
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
    content.append({"type": "image_url", "image_url": {"url": image_url}})

    payload = {"model": VERIFY_MODEL, "messages": [{"role": "user", "content": content}]}

    last_error = "unknown"
    for attempt in range(1, MAX_VERIFY_CALL_ATTEMPTS + 1):
        rate_limit_verify(lane)
        try:
            response = requests.post(CHAT_URL, headers=VERIFY_HEADERS[lane], json=payload, timeout=200)
        except requests.exceptions.RequestException as e:
            last_error = f"network error: {e}"
            continue
        if not response.ok:
            last_error = f"http {response.status_code}: {response.text[:300]}"
            continue
        try:
            raw = response.json()["choices"][0]["message"]["content"]
            raw = _clean_json_response(raw)
            result = json.loads(raw)
        except Exception as e:
            last_error = f"parse error: {e}"
            continue

        if have_refs:
            score = result.get("identity_similarity_score")
            result["identity_similarity_ok"] = (
                isinstance(score, (int, float)) and score >= MIN_IDENTITY_SIMILARITY_SCORE
            )

        return result, None

    return None, last_error


def verification_passed(result):
    if result is None:
        return None
    required_keys = ["count_ok", "role_match_ok", "outfit_match_ok",
                      "style_match_ok", "height_proportion_ok", "era_consistency_ok",
                      "pose_action_ok"]
    for optional_key in ("identity_similarity_ok", "background_match_ok", "props_match_ok"):
        if optional_key in result:
            required_keys.append(optional_key)
    return all(result.get(key) is True for key in required_keys)