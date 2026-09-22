# ============================================================
# FILE: image_core.py
# CHANGE: added a GLOBAL cooldown for generation, but only
# REACTIVE — it does nothing during a healthy run. It only
# activates the moment Agnes returns a 503 ("queue is full"),
# at which point EVERY gen lane backs off together for a real
# amount of time before anyone retries — instead of every lane
# instantly walking back into the same full queue, which is what
# caused the endless retry loop at 85-scene scale.
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SECONDS_BETWEEN_REQUESTS = 20
SECONDS_BETWEEN_VERIFY = 20
MAX_VERIFY_CALL_ATTEMPTS = 3
MAX_BLIND_RETRIES = 3
MAX_BRAIN_RETRIES = 3

REFERENCE_CACHE_RETRY_ATTEMPTS = 3
REFERENCE_CACHE_RETRY_DELAY_SECONDS = 3

GEN_COOLDOWN_ON_QUEUE_FULL_SECONDS = 45   # how long ALL gen lanes back off after a 503

IMAGE_MODEL = "agnes-image-2.1-flash"
VERIFY_MODEL = "agnes-2.0-flash"
IMAGE_SIZE = "1024x768"

SCENES_FILE = os.path.join(BASE_DIR, "scenes.txt")
FRAMES_FILE = os.path.join(BASE_DIR, "frames.txt")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "generated_images")
MANIFEST_FILE = os.path.join(BASE_DIR, "manifest.json")
REFERENCE_CHARACTERS_FILE = os.path.join(BASE_DIR, "reference_characters.json")

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
    remaining_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("CHARACTERS:"):
            character_names = [n.strip() for n in stripped.split(":", 1)[1].split(",") if n.strip()]
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

    return {"characters": character_names, "text": scene_text, "frame_id": frame_id}


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
        scenes.append((scene_number, parsed["characters"], parsed["text"]))

    return scenes


def get_current_scene_keys():
    scenes = load_scenes(SCENES_FILE)
    return {f"scene_{number:03d}" for number, _, _ in scenes}


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


def build_prompt(characters, scene_text, extra_instruction=""):
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


def generate_image_url(lane, characters, scene_text, extra_instruction=""):
    prompt = build_prompt(characters, scene_text, extra_instruction)
    reference_urls = [REFERENCE_IMAGES[name] for name in characters if name in REFERENCE_IMAGES]

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


def verify_image(lane, image_url, characters, scene_text):
    character_list = ", ".join(c.replace("_", " ") for c in characters)
    named = [c for c in characters if c in REFERENCE_IMAGES]
    ref_data_uris = [REFERENCE_IMAGES_B64.get(c) for c in named]
    have_refs = bool(named) and all(ref_data_uris)
    positions = get_position_labels(len(named))

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

    if have_refs:
        intro = f"""You are given {len(named)} reference image(s) followed by ONE
generated scene image (the LAST image). Compare directly against the
reference(s) — do not guess from the text alone.

{ref_labels}
- The final image is the GENERATED SCENE to check.{crowd_note}
{identity_check}"""
    else:
        intro = "Look at this generated scene image and check it against the description below."

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

Respond ONLY with JSON, no other text:
{{"count_ok": true/false, "role_match_ok": true/false,
"outfit_match_ok": true/false, "style_match_ok": true/false,
"height_proportion_ok": true/false, "era_consistency_ok": true/false,
"pose_action_ok": true/false,
"issue": "<name the failing check(s) and describe briefly, or 'none'>"}}
"""

    content = [{"type": "text", "text": check_prompt}]
    if have_refs:
        for data_uri in ref_data_uris:
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
            raw = raw.strip().strip("```json").strip("```").strip()
            return json.loads(raw), None
        except Exception as e:
            last_error = f"parse error: {e}"
            continue

    return None, last_error


def verification_passed(result):
    if result is None:
        return None
    required_keys = ["count_ok", "role_match_ok", "outfit_match_ok",
                      "style_match_ok", "height_proportion_ok", "era_consistency_ok",
                      "pose_action_ok"]
    return all(result.get(key) is True for key in required_keys)