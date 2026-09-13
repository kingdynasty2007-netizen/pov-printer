# ============================================================
# FILE: video_core.py
# PURPOSE: Shared logic for video generation. Submits across ALL
# video keys, polls everything together, auto-retries failures.
# GLOBAL account-wide rate limiting (Agnes's 429 turned out to be
# shared, not per-key).
# ============================================================

import os
import re
import sys
import json
import time
import itertools
import threading
import requests
from dotenv import load_dotenv

load_dotenv()
#----updated file path so it will read better ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_FILE = os.path.join(BASE_DIR, "manifest.json")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "generated_videos")
VIDEO_SCENES_FILE = os.path.join(BASE_DIR, "video_scenes.txt")
IMAGE_SCENES_FILE = os.path.join(BASE_DIR, "scenes.txt")


SECONDS_BETWEEN_VIDEO_REQUESTS = 20
GLOBAL_SECONDS_BETWEEN_VIDEO_SUBMISSIONS = 10   # paces ALL keys combined
POLL_INTERVAL = 5
POLL_TIMEOUT = 900
MAX_VIDEO_RETRIES = 3
RETRY_ROUND_DELAY = 15

VIDEO_MODEL = "agnes-video-v2.0"
VIDEO_DURATION = 10
VIDEO_FPS = 24
VIDEO_WIDTH = 1152
VIDEO_HEIGHT = 768
VIDEO_INFERENCE_STEPS = 8

MANIFEST_FILE = "manifest.json"
OUTPUT_FOLDER = "generated_videos"
VIDEO_SCENES_FILE = "video_scenes.txt"
IMAGE_SCENES_FILE = "scenes.txt"

CREATE_VIDEO_URL = "https://apihub.agnes-ai.com/v1/videos"
RESULT_VIDEO_URL = "https://apihub.agnes-ai.com/agnesapi"

DEFAULT_MOTION_PROMPT = """
Animate the scene with simple, grounded, natural motion.
Subtle breathing and small natural body movements.
Very subtle movement of hair and clothing from a gentle breeze.
Use restrained, cinematic camera movement appropriate to the
original composition.

Preserve the exact identity, facial proportions, hairstyle,
clothing, body proportions, environment, lighting, color grading,
and artistic style of the source image.

Do not redesign the character. Do not change clothing. Do not
change facial structure. Do not introduce new characters. Do not
merge characters. Do not swap identities, roles, clothing, or
physical traits.

No exaggerated gestures. No cartoon physics. No unnatural body
movement. No camera shake. No sudden camera movement. No style
drift. No text. No watermark. No logo.
"""
# KNOWN OPEN ISSUE (not yet fixed): this blanket-bans gestures while
# many scenes describe one. See the handoff PDF, issue #22 — needs
# to be reworded to "animate only the specific action in the scene
# text" instead of a blanket ban, and paired with real per-scene
# video_scenes.txt entries for any scene with a described gesture.

os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def _collect_keys(prefix):
    found = {}
    for name, value in os.environ.items():
        match = re.match(rf"^{prefix}(\d+)$", name)
        if match and value:
            found[int(match.group(1))] = value
    return {f"VID{n}": found[n] for n in sorted(found)}


VIDEO_KEYS = _collect_keys("AGNES_VIDEO_KEY_")

if not VIDEO_KEYS:
    print("❌ No video keys found. Add AGNES_VIDEO_KEY_1=... to your .env file")
    sys.exit(1)

VIDEO_HEADERS = {
    lane: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for lane, key in VIDEO_KEYS.items()
}
print(f"🔑 Loaded {len(VIDEO_KEYS)} video key(s): {', '.join(VIDEO_KEYS)}")


_lane_last_time = {lane: 0 for lane in VIDEO_KEYS}
_lane_locks = {lane: threading.Lock() for lane in VIDEO_KEYS}

def rate_limit_lane(lane):
    with _lane_locks[lane]:
        wait = SECONDS_BETWEEN_VIDEO_REQUESTS - (time.time() - _lane_last_time[lane])
        if wait > 0:
            time.sleep(wait)
        _lane_last_time[lane] = time.time()


_global_lock = threading.Lock()
_global_last_submit_time = 0
_global_cooldown_until = 0

def rate_limit_global():
    global _global_last_submit_time
    with _global_lock:
        now = time.time()
        wait_cooldown = _global_cooldown_until - now
        if wait_cooldown > 0:
            time.sleep(wait_cooldown)
            now = time.time()
        wait_pacing = GLOBAL_SECONDS_BETWEEN_VIDEO_SUBMISSIONS - (now - _global_last_submit_time)
        if wait_pacing > 0:
            time.sleep(wait_pacing)
        _global_last_submit_time = time.time()

def trigger_global_cooldown(seconds):
    global _global_cooldown_until
    with _global_lock:
        candidate = time.time() + seconds
        if candidate > _global_cooldown_until:
            _global_cooldown_until = candidate


class VideoRateLimitError(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def classify_error(error_text):
    text = (error_text or "").lower()
    if any(s in text for s in ["content_policy", "content policy", "moderation", "flagged", "violates"]):
        return "content_policy"
    if any(s in text for s in ["network error", "timeout", "connection reset", "connection aborted",
                                "http 500", "http 502", "http 503", "http 504", "upstream_error"]):
        return "transient"
    return "other"


def load_video_scene_overrides():
    if not os.path.exists(VIDEO_SCENES_FILE):
        return {}
    with open(VIDEO_SCENES_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    overrides = {}
    for block in content.split("SCENE:"):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        scene_key = lines[0].strip()
        prompt_text = "\n".join(lines[1:]).strip()
        if scene_key and prompt_text:
            overrides[scene_key] = prompt_text
    return overrides


def get_current_scene_keys(scenes_file=IMAGE_SCENES_FILE):
    if not os.path.exists(scenes_file):
        return set()
    with open(scenes_file, "r", encoding="utf-8") as f:
        content = f.read()
    raw_blocks = [b.strip() for b in content.split("---") if b.strip()]
    keys = set()
    for position, block in enumerate(raw_blocks, start=1):
        frame_match = re.search(r"FRAME\s+0*?(\d+)\s*:", block, re.IGNORECASE)
        number = int(frame_match.group(1)) if frame_match else position
        keys.add(f"scene_{number:03d}")
    return keys


def build_video_prompt(scene_text, motion_override=None):
    motion = motion_override if motion_override else DEFAULT_MOTION_PROMPT
    return f"""
SOURCE IMAGE:
The supplied image is the authoritative visual source for this
video. Animate the existing scene rather than redesigning it.

The scene description is:
{scene_text}

MOTION:
{motion}
"""


def calculate_frames(duration, fps):
    target = duration * fps
    n = round((target - 1) / 8)
    frames = (8 * n) + 1
    return max(1, min(frames, 441))


def submit_video_task(lane, image_url, scene_text, motion_override=None):
    prompt = build_video_prompt(scene_text, motion_override)
    num_frames = calculate_frames(VIDEO_DURATION, VIDEO_FPS)

    payload = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "image": image_url,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "num_frames": num_frames,
        "frame_rate": VIDEO_FPS,
        "num_inference_steps": VIDEO_INFERENCE_STEPS,
    }

    rate_limit_global()
    rate_limit_lane(lane)

    try:
        response = requests.post(CREATE_VIDEO_URL, headers=VIDEO_HEADERS[lane], json=payload, timeout=120)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Connection failed: {e}")

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise VideoRateLimitError(f"HTTP 429: {response.text[:300]}", retry_after=retry_after)

    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

    data = response.json()
    video_id = data.get("video_id")
    if not video_id:
        raise RuntimeError(f"No video_id returned: {data}")

    return video_id, num_frames


def find_video_url(status_data):
    metadata = status_data.get("metadata")
    if isinstance(metadata, dict) and metadata.get("url"):
        return metadata["url"]
    for key in ("url", "video_url", "output_url"):
        if status_data.get(key):
            return status_data[key]
    task_id = status_data.get("task_id") or status_data.get("id")
    if task_id:
        return f"https://platform-outputs.agnes-ai.space/videos/agnes-video-v2.0/{task_id}.mp4"
    return None


def poll_all_pending(pending, lane_by_video_id, board=None):
    results = {}
    start_time = time.time()

    while pending:
        if time.time() - start_time > POLL_TIMEOUT:
            for video_id, scene_key in list(pending.items()):
                results[video_id] = {"status": "failed", "error": "timeout waiting for video"}
                if board:
                    board.update(scene_key, "render timed out")
                pending.pop(video_id)
            break

        for video_id in list(pending):
            lane = lane_by_video_id[video_id]
            scene_key = pending[video_id]
            try:
                response = requests.get(
                    RESULT_VIDEO_URL, params={"video_id": video_id},
                    headers=VIDEO_HEADERS[lane], timeout=60,
                )
                if not response.ok:
                    continue

                data = response.json()
                status = data.get("status")

                if status == "completed":
                    results[video_id] = {"status": "completed", "url": find_video_url(data)}
                    if board:
                        board.update(scene_key, "rendered, downloading")
                    pending.pop(video_id)
                elif status == "failed":
                    results[video_id] = {"status": "failed", "error": str(data)}
                    if board:
                        board.update(scene_key, f"render failed: {str(data)[:60]}")
                    pending.pop(video_id)

            except requests.exceptions.RequestException:
                continue

        if pending:
            time.sleep(POLL_INTERVAL)

    return results


def download_video(video_url, output_path):
    response = requests.get(video_url, timeout=300)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)


def get_next_video_path(scene_key, prefix="", suffix=""):
    version = 1
    while True:
        filename = f"{prefix}{scene_key}{suffix}_v{version:03d}.mp4"
        path = os.path.join(OUTPUT_FOLDER, filename)
        if not os.path.exists(path):
            return path
        version += 1


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


def generate_videos(scenes, board, overrides=None, path_prefix=""):
    overrides = overrides or {}
    remaining = list(scenes)
    final_results = {}
    max_attempts = MAX_VIDEO_RETRIES + 1
    round_number = 0

    while remaining and round_number < max_attempts:
        round_number += 1
        lane_cycle = itertools.cycle(VIDEO_KEYS)
        pending = {}
        lane_by_video_id = {}
        scene_by_video_id = {}
        scene_lookup = {key: entry for key, entry in remaining}
        next_round = []

        for scene_key, entry in remaining:
            lane = next(lane_cycle)
            motion = overrides.get(scene_key)
            label = "submitting" if round_number == 1 else f"resubmitting (round {round_number}/{max_attempts})"
            board.update(scene_key, label)

            try:
                video_id, _ = submit_video_task(lane, entry["url"], entry.get("scene_text", ""), motion)
                pending[video_id] = scene_key
                lane_by_video_id[video_id] = lane
                scene_by_video_id[video_id] = scene_key
                board.update(scene_key, "rendering")
            except VideoRateLimitError as e:
                wait_for = float(e.retry_after) if e.retry_after else 30
                board.update(scene_key, f"rate limited — cooling down {int(wait_for)}s")
                trigger_global_cooldown(wait_for)
                next_round.append((scene_key, entry))
            except Exception as e:
                error_text = str(e)
                category = classify_error(error_text)
                board.update(scene_key, f"submit failed ({category}): {error_text[:50]}")
                next_round.append((scene_key, entry))

        if pending:
            results = poll_all_pending(pending, lane_by_video_id, board=board)

            for video_id, result in results.items():
                scene_key = scene_by_video_id[video_id]
                entry = scene_lookup[scene_key]

                if result["status"] == "completed" and result.get("url"):
                    output_path = get_next_video_path(scene_key, prefix=path_prefix)
                    downloaded = False
                    last_dl_error = None

                    for dl_attempt in range(1, 4):
                        try:
                            download_video(result["url"], output_path)
                            downloaded = True
                            break
                        except Exception as e:
                            last_dl_error = str(e)
                            time.sleep(3)

                    if downloaded:
                        update_manifest_entry(scene_key, {
                            "video": {"url": result["url"], "local_path": output_path, "status": "ok"}
                        })
                        board.update(scene_key, "✅ downloaded", done=True)
                        final_results[scene_key] = {"status": "ok", "local_path": output_path}
                    else:
                        board.update(scene_key, f"download failed 3x: {last_dl_error[:40]} → regenerating")
                        next_round.append((scene_key, entry))
                else:
                    next_round.append((scene_key, entry))

        remaining = next_round
        if remaining and round_number < max_attempts:
            time.sleep(RETRY_ROUND_DELAY)

    for scene_key, entry in remaining:
        update_manifest_entry(scene_key, {"video": {"status": "failed", "error": "exhausted all retry rounds"}})
        board.update(scene_key, "❌ FAILED after all retries", done=True)
        final_results[scene_key] = {"status": "failed"}

    return final_results