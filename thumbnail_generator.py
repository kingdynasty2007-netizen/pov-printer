# ============================================================
# FILE: thumbnail_generator.py
# PURPOSE: Generates the YouTube thumbnail (1280x720) using the
# thumbnail plan from run_metadata() + existing character refs as
# visual anchors, so the thumbnail face matches the video.
#
# Standalone file (not folded into ref_character_generator.py)
# because it's a single image with a different shape: one prompt
# built from thumbnail.json + reference_characters.json, not a
# loop over N reference items.
#
# Paths come from run_paths, so with POV_RUN_ID set it reads/writes
# data/productions/<RUN-ID>/ and never touches the shared root
# folders. Legacy layout is used when POV_RUN_ID is unset.
#
# INPUT:  reference_characters.json (for face match), and
#         metadata/thumbnail.json (written by run_metadata()).
# OUTPUT: final/thumbnail.png, uploaded to ImgBB, URL written to
#         final/thumbnail_url.txt.
# RUN: python thumbnail_generator.py
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

load_dotenv()

_P = run_paths.get_paths()
REFERENCE_CHARACTERS_FILE = _P["ref_characters_file"]
THUMBNAIL_PLAN_FILE = os.path.join(_P["metadata_dir"], "thumbnail.json")
# Saved next to the final video, NOT in the scene images folder —
# image_core.OUTPUT_FOLDER is that folder, and a non-scene file
# landing there confuses the image pipeline's directory.
OUTPUT_PATH = os.path.join(os.path.dirname(_P["final_output"]), "thumbnail.png")
OUTPUT_URL_FILE = os.path.join(os.path.dirname(_P["final_output"]), "thumbnail_url.txt")

IMAGE_MODEL = "agnes-image-2.1-flash"
VERIFY_MODEL = "agnes-2.0-flash"
IMAGE_SIZE = "1280x720"   # YouTube's required thumbnail size, not the 16:9 scene size
IMAGE_URL = "https://apihub.agnes-ai.com/v1/images/generations"
CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

MAX_ATTEMPTS = 4
NETWORK_RETRY_ATTEMPTS = 3
SECONDS_BETWEEN_REQUESTS = 20
COOLDOWN_AFTER_LIMIT_SECONDS = 30

os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)


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

import itertools
_gen_cycle = itertools.cycle(GEN_KEYS.keys())
_verify_cycle = itertools.cycle(VERIFY_KEYS.keys())
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
    return next(_gen_cycle)

def _next_verify_lane():
    return next(_verify_cycle)

def _pace(lane, last_time_dict):
    _apply_cooldown()
    with _pace_lock:
        wait = SECONDS_BETWEEN_REQUESTS - (time.time() - last_time_dict.get(lane, 0))
        if wait > 0:
            time.sleep(wait)
        last_time_dict[lane] = time.time()


def _guess_mime(path_or_url):
    return "image/png" if path_or_url.lower().endswith(".png") else "image/jpeg"

def _fetch_as_data_uri(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    b64 = base64.b64encode(response.content).decode("utf-8")
    return f"data:{_guess_mime(url)};base64,{b64}"

def local_image_to_data_uri(path):
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{_guess_mime(path)};base64,{b64}"


def _clean_json_response(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def load_thumbnail_plan():
    if not os.path.exists(THUMBNAIL_PLAN_FILE):
        print(f"❌ No thumbnail plan found at {THUMBNAIL_PLAN_FILE}")
        print("   Run production_manager.py's metadata stage first.")
        sys.exit(1)
    with open(THUMBNAIL_PLAN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_reference_characters():
    if not os.path.exists(REFERENCE_CHARACTERS_FILE):
        return {}
    with open(REFERENCE_CHARACTERS_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else {}


def _normalize_name(text):
    """'Good Samaritan' -> 'good_samaritan'. The thumbnail plan's
    characters are free-form display names written by the LLM, while
    reference keys are snake_case lower, so a direct dict lookup never
    matches and the face anchors silently drop off."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", (text or "").lower())
    return cleaned.strip("_")


def _match_reference_names(plan_characters, ref_names):
    """
    Map the plan's display names onto real reference keys.

    The plan says e.g. "Good Samaritan" but the reference key is
    "samaritan", so a straight lookup finds nothing. Match on tokens
    instead: a reference key matches when ALL of its tokens appear in
    the plan name. The longest matching key wins, so "wounded traveler"
    resolves to "traveler" rather than a shorter accidental match.
    Returns (matched_keys, unmatched_labels).
    """
    by_normalized = {}
    for key in ref_names:
        by_normalized.setdefault(_normalize_name(key), key)

    matched, unmatched = [], []
    for label in plan_characters or []:
        norm = _normalize_name(label)
        if not norm:
            continue
        if norm in by_normalized:
            matched.append(by_normalized[norm])
            continue
        label_tokens = set(norm.split("_"))
        candidates = [
            key for k_norm, key in by_normalized.items()
            if k_norm and set(k_norm.split("_")).issubset(label_tokens)
        ]
        if candidates:
            matched.append(max(candidates, key=lambda k: len(_normalize_name(k))))
        else:
            unmatched.append(label)
    return matched, unmatched


def build_prompt(plan, ref_names):
    concept = plan.get("concept") or plan.get("description") or ""
    characters = plan.get("characters") or []
    # The real field written by production_manager._generate_thumbnail_plan()
    # is "optional_text" — not "text_overlay"/"title_text".
    text_overlay = plan.get("optional_text") or ""

    named, unmatched = _match_reference_names(characters, ref_names)
    if not characters:
        # No named characters in the plan — fall back to any references we have.
        named = list(ref_names)
        unmatched = []

    lines = [
        "Generate a YouTube THUMBNAIL, not a scene frame. High contrast, "
        "bold, eye-catching composition, dramatic lighting, designed to "
        "be legible as a small preview image.",
    ]
    if named:
        lines.append(
            f"Featured character(s): {', '.join(n.replace('_',' ') for n in named)}. "
            "Match their face, hair, and build exactly to their reference image(s) "
            "provided. Pose and expression should match the concept below, NOT the "
            "reference image's pose."
        )
    if concept:
        lines.append(f"CONCEPT: {concept}")
    if text_overlay:
        lines.append(
            f"Leave clear, uncluttered space for bold text overlay reading: "
            f"\"{text_overlay}\" — do not render the text yourself, just compose "
            f"around it."
        )
    return "\n\n".join(lines), named, unmatched


def generate_thumbnail_url(prompt, ref_urls, previous_issue=None):
    full_prompt = prompt
    if previous_issue:
        full_prompt += (
            f"\n\nCRITICAL — the previous attempt FAILED because: "
            f"\"{previous_issue}\". Correct this specifically."
        )

    payload = {
        "model": IMAGE_MODEL,
        "prompt": full_prompt,
        "size": IMAGE_SIZE,
        "extra_body": {"image": ref_urls, "response_format": "url"},
    }

    last_error = "unknown"
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
            last_error = f"[{lane}] HTTP {response.status_code}: {response.text[:200]}"
            continue
        data = response.json()
        try:
            return data["data"][0]["url"]
        except (KeyError, IndexError):
            last_error = f"[{lane}] no url in response: {data}"
            continue
    raise RuntimeError(last_error)


def verify_thumbnail(image_url, named_refs, ref_data_uris, concept):
    ref_note = ""
    if named_refs:
        ref_note = (
            f"\nThe reference image(s) show: {', '.join(n.replace('_',' ') for n in named_refs)}. "
            "Compare the candidate's face/hair/build against them."
        )
    check_prompt = f"""
This is a YouTube thumbnail check. The candidate is the LAST image attached.
Concept it should match: {concept or "(no concept given)"}
{ref_note}

Check:
1. legible_ok: would this read clearly as a small preview thumbnail (high contrast, not muddy/cluttered)?
2. identity_match_ok: if reference character(s) given, does the candidate match them closely? If no references given, mark true.
3. composition_ok: is there clean visual space that wouldn't be ruined by a text overlay?
4. era_consistency_ok: no anachronistic/modern elements if the concept implies a historical setting?

Respond ONLY with JSON:
{{"legible_ok": true/false, "identity_match_ok": true/false, "composition_ok": true/false,
"era_consistency_ok": true/false, "issue": "<specific problem, or 'none'>"}}
"""
    content = [{"type": "text", "text": check_prompt}]
    for uri in ref_data_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})
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
            required = ["legible_ok", "identity_match_ok", "composition_ok", "era_consistency_ok"]
            passed = all(result.get(k) is True for k in required)
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


def upload_to_imgbb(image_path):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    response = requests.post(
        IMGBB_UPLOAD_URL,
        data={"key": _imgbb_key, "image": image_data, "name": "thumbnail"},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"ImgBB error {response.status_code}: {response.text[:200]}")
    data = response.json()
    try:
        return data["data"]["url"]
    except (KeyError, TypeError):
        raise RuntimeError(f"No URL in ImgBB response: {data}")


def main():
    print("\n=== THUMBNAIL GENERATOR ===\n")

    plan = load_thumbnail_plan()
    ref_characters = load_reference_characters()

    prompt, named_refs, unmatched = build_prompt(plan, list(ref_characters.keys()))
    ref_urls = [ref_characters[n] for n in named_refs]

    print(f"🖼️  Featured character(s): {', '.join(named_refs) or '(none — background/concept only)'}")
    if unmatched:
        print(f"ℹ️  No reference image for: {', '.join(unmatched)} — "
              f"those faces will not be matched to the video")

    ref_data_uris = []
    for n in named_refs:
        try:
            ref_data_uris.append(_fetch_as_data_uri(ref_characters[n]))
        except Exception as e:
            print(f"⚠️  Could not cache reference for {n} for verification: {e}")

    previous_issue = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"🎨 Generating (attempt {attempt}/{MAX_ATTEMPTS})...")
        try:
            url = generate_thumbnail_url(prompt, ref_urls, previous_issue=previous_issue)
        except Exception as e:
            print(f"❌ Generation failed: {e}")
            previous_issue = None
            continue

        print("🔎 Verifying...")
        passed, issue = verify_thumbnail(url, named_refs, ref_data_uris, plan.get("concept") or plan.get("description"))

        if passed:
            download_image(url, OUTPUT_PATH)
            imgbb_url = upload_to_imgbb(OUTPUT_PATH)
            with open(OUTPUT_URL_FILE, "w", encoding="utf-8") as f:
                f.write(imgbb_url)
            print(f"\n✅ Thumbnail ready: {OUTPUT_PATH}")
            print(f"   ImgBB: {imgbb_url}")
            return
        else:
            print(f"❌ Rejected: {issue}")
            previous_issue = str(issue)

    print(f"\n❌ Thumbnail FAILED after {MAX_ATTEMPTS} attempts. Last issue: {previous_issue}")
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")
