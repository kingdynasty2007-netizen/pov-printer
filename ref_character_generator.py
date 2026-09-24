# ============================================================
# FILE: ref_character_generator.py
# CHANGES:
#   - Background-bleed fix: IMAGE_STYLE text is now explicitly
#     scoped to rendering technique only, with a hard override
#     telling the model to ignore any setting/location language and
#     keep the background plain regardless. New background_plain_ok
#     verification check backs this up.
#   - Parallelized: character 1 processes alone (it becomes the
#     style anchor), then every character after it runs in its own
#     thread simultaneously — real speedup for 3+ characters.
#   - Cross-character style consistency: character 1's actual image
#     becomes a verification-time style anchor for every character
#     after it (new style_vs_anchor_ok check). Anchor is NOT fed into
#     generation — only verification — to avoid identity bleed
#     between different characters.
#   - Lane-cycling (_next_gen_lane/_next_verify_lane) is now lock-
#     protected — needed now that multiple worker threads call it
#     concurrently, which wasn't true before this rewrite.
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
from db import supabase
from status_board import StatusBoard

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REF_PROMPTS_FILE = os.path.join(BASE_DIR, "ref_prompts.json")
REF_OUTPUT_FOLDER = os.path.join(BASE_DIR, "ref_images")
REFERENCE_CHARACTERS_FILE = os.path.join(BASE_DIR, "reference_characters.json")
SCRIPTFORMAT_FOLDER = os.path.join(BASE_DIR, "scriptformat")

IMAGE_MODEL = "agnes-image-2.1-flash"
IMAGE_SIZE = "768x1024"
IMAGE_URL = "https://apihub.agnes-ai.com/v1/images/generations"
CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
VERIFY_MODEL = "agnes-2.0-flash"

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

MAX_ATTEMPTS_PER_CHARACTER = 3
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
    # Remove trailing separator lines from comment headers
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


def generate_ref_image(character_name, prompt, image_style_text, previous_issue=None):
    style_instruction = ""
    if image_style_text:
        style_instruction = f"""

VISUAL RENDERING STYLE (apply ONLY the art/rendering technique described
below — line work, shading approach, coloring, illustration technique):
{image_style_text}

CRITICAL OVERRIDE: Ignore any mention of specific locations, buildings,
architecture, streets, objects, or environmental scenery in the style
text above. This is a character reference sheet, NOT a scene — it MUST
have a plain, minimal, undecorated background regardless of any setting
described above. Do not render buildings, furniture, landscape, or any
environmental detail."""

    base_instruction = (
        "IMPORTANT: Front-facing portrait, VERTICAL/PORTRAIT orientation. "
        "Character standing straight, arms relaxed at sides, neutral "
        "expression, hands empty, no props. The ENTIRE body must be "
        "visible in frame — head, torso, legs, AND both feet, with the "
        "camera positioned far enough back to fit the whole standing "
        "figure. Leave visible empty margin above the head and below the "
        "feet. This is a CHARACTER REFERENCE — full-body framing and a "
        "plain background matter more than drama or close-up detail."
        f"{style_instruction}"
    )

    if previous_issue:
        base_instruction += (
            f"\n\nCRITICAL — the previous attempt FAILED because: "
            f"\"{previous_issue}\". Correct this specifically."
        )

    full_prompt = f"{prompt}\n\n{base_instruction}"
    payload = {"model": IMAGE_MODEL, "prompt": full_prompt, "size": IMAGE_SIZE, "extra_body": {"response_format": "url"}}

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


def verify_reference_image(image_url, character_name, image_style_text, anchor_data_uri=None):
    style_block = image_style_text if image_style_text else "No specific style requirement given — mark style_match_ok true by default."

    anchor_note = ""
    extra_check_line = ""
    extra_json_field = ""
    if anchor_data_uri:
        anchor_note = """
The FIRST image attached is a STYLE ANCHOR — the established illustration
style already used for a DIFFERENT character in this same story. Ignore
that character's identity/face/clothing entirely — only compare rendering
technique (line weight, shading approach, color saturation, overall
illustration style). The FINAL image is the candidate to check.
"""
        extra_check_line = "\n7. style_vs_anchor_ok: does the candidate's rendering technique closely match the anchor image? Flag ANY noticeable stylistic difference."
        extra_json_field = ',\n"style_vs_anchor_ok": true/false'
    else:
        anchor_note = "\nThe attached image is the candidate to check.\n"

    check_prompt = f"""
This is a character reference sheet check for "{character_name}".
{anchor_note}
Required visual style for this image:
{style_block}

Check:
1. person_count_ok: exactly ONE person visible, no extras?
2. front_facing_ok: facing forward, not a side profile or back view?
3. neutral_pose_ok: standing straight, arms relaxed, hands empty, no props?
4. full_body_ok: full body visible from head to feet, not cropped?
5. style_match_ok: does the rendering style match the required visual
   style above? Be strict — photorealistic FAILS if illustration was
   required, and vice versa. If no style was given, mark true by default.
6. background_plain_ok: is the background plain, minimal, and
   undecorated — NO buildings, furniture, landscape, or environmental
   scenery — even if the style text mentions a setting?{extra_check_line}

If ANY check above is false, describe in "issue" the SPECIFIC VISUAL
PROBLEM you actually see — not the name of the check. Bad: "full_body_ok".
Good: "feet and lower legs are cropped out of frame" or "the shading is
flat/cel-shaded rather than the soft gradients required". Be concrete
enough that someone who cannot see the image would know exactly what
is wrong.

Respond ONLY with JSON:
{{"person_count_ok": true/false, "front_facing_ok": true/false,
"neutral_pose_ok": true/false, "full_body_ok": true/false,
"style_match_ok": true/false, "background_plain_ok": true/false{extra_json_field},
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
            raw = raw.strip().strip("```json").strip("```").strip()
            result = json.loads(raw)
            required = ["person_count_ok", "front_facing_ok", "neutral_pose_ok",
                        "full_body_ok", "style_match_ok", "background_plain_ok"]
            if anchor_data_uri:
                required.append("style_vs_anchor_ok")
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


def upload_to_imgbb(image_path, character_name):
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    response = requests.post(
        IMGBB_UPLOAD_URL,
        data={"key": _imgbb_key, "image": image_data, "name": f"ref_{character_name}"},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"ImgBB error {response.status_code}: {response.text[:200]}")
    data = response.json()
    try:
        return data["data"]["url"]
    except (KeyError, TypeError):
        raise RuntimeError(f"No URL in ImgBB response: {data}")


def save_ref_to_db(name, style, prompt, imgbb_url, local_path):
    try:
        supabase.table("ref_characters").insert({
            "name": name, "style": style, "prompt": prompt,
            "imgbb_url": imgbb_url, "local_path": local_path,
        }).execute()
    except Exception as e:
        print(f"   ⚠️  Could not save to Supabase: {e}")


def write_reference_characters_json(ref_map):
    with open(REFERENCE_CHARACTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(ref_map, f, indent=2)
    print(f"✅ Written: {REFERENCE_CHARACTERS_FILE}")


board = None


def process_one_character(character_name, prompt, image_style_text, anchor_state, style):
    local_path = os.path.join(REF_OUTPUT_FOLDER, f"{character_name}.png")
    previous_issue = None

    for attempt in range(1, MAX_ATTEMPTS_PER_CHARACTER + 1):
        board.update(character_name, f"generating (attempt {attempt}/{MAX_ATTEMPTS_PER_CHARACTER})")
        try:
            agnes_url = generate_ref_image(character_name, prompt, image_style_text, previous_issue=previous_issue)
        except Exception as e:
            board.update(character_name, f"generation failed: {str(e)[:60]}")
            previous_issue = None
            continue

        board.update(character_name, "verifying")
        with anchor_state["lock"]:
            anchor_data_uri = anchor_state["data_uri"]
        passed, issue = verify_reference_image(agnes_url, character_name, image_style_text, anchor_data_uri=anchor_data_uri)

        if passed:
            board.update(character_name, "downloading")
            try:
                download_image(agnes_url, local_path)
            except Exception as e:
                board.update(character_name, f"download failed: {str(e)[:60]}")
                continue

            board.update(character_name, "uploading to ImgBB")
            try:
                imgbb_url = upload_to_imgbb(local_path, character_name)
            except Exception as e:
                board.update(character_name, f"ImgBB upload failed: {str(e)[:60]}")
                continue

            save_ref_to_db(character_name, style or "unknown", prompt, imgbb_url, local_path)
            board.update(character_name, "✅ ready", done=True)

            with anchor_state["lock"]:
                if anchor_state["data_uri"] is None:
                    anchor_state["data_uri"] = local_image_to_data_uri(local_path)

            return character_name, imgbb_url
        else:
            board.update(character_name, f"rejected: {str(issue)[:60]}")
            previous_issue = str(issue)

    board.update(character_name, "❌ FAILED after all attempts", done=True)
    return character_name, None


def main():
    global board

    print("\n=== REFERENCE CHARACTER GENERATOR ===\n")

    if not os.path.exists(REF_PROMPTS_FILE):
        print(f"❌ {REF_PROMPTS_FILE} not found — run script_engine.py first")
        sys.exit(1)

    with open(REF_PROMPTS_FILE, "r", encoding="utf-8") as f:
        ref_data = json.load(f)

    if "characters" in ref_data:
        style = ref_data.get("style")
        ref_prompts = ref_data["characters"]
    else:
        style = None
        ref_prompts = ref_data

    if not ref_prompts:
        print("❌ No characters found in ref_prompts.json — nothing to generate")
        sys.exit(1)

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

    if image_style_text:
        for name in ref_prompts:
            ref_prompts[name] = ref_prompts[name].replace("[style-appropriate description]", image_style_text)
        print(f"🔧 Replaced [style-appropriate description] with actual style text in {len(ref_prompts)} prompt(s)")

    items = list(ref_prompts.items())
    print(f"📋 Found {len(items)} character(s): {', '.join(ref_prompts.keys())}\n")

    board = StatusBoard(list(ref_prompts.keys()))
    board.start()

    anchor_state = {"data_uri": None, "lock": threading.Lock()}
    successful_refs = {}

    # First character processes ALONE — it becomes the style anchor for
    # everyone else, so there's nothing to compare against until it's done.
    first_name, first_prompt = items[0]
    name, url = process_one_character(first_name, first_prompt, image_style_text, anchor_state, style)
    if url:
        successful_refs[name] = url

    # Everyone else runs in parallel — real speedup starts at 3+ characters.
    rest = items[1:]
    results_lock = threading.Lock()
    threads = []

    def worker(name, prompt):
        result_name, result_url = process_one_character(name, prompt, image_style_text, anchor_state, style)
        if result_url:
            with results_lock:
                successful_refs[result_name] = result_url

    for name, prompt in rest:
        t = threading.Thread(target=worker, args=(name, prompt), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    board.stop()

    if not successful_refs:
        print("\n❌ No reference images were generated successfully")
        sys.exit(1)

    write_reference_characters_json(successful_refs)
    print(f"\n✅ Done — {len(successful_refs)}/{len(items)} reference(s) ready")
    print("   Next: batch_image_generator.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")