# ============================================================
# FILE: audio_core.py
# PURPOSE: Narration/TTS via Gemini's REST generateContent endpoint
# (NOT the Live/WebSocket API — that hung on this network). IPv4
# forced (broken outbound IPv6 caused silent infinite hangs).
#
# INCLUDES A FIX NOT PREVIOUSLY DELIVERED: load_narration() now
# hard-errors if audio_scenes.txt exists but zero SCENE: blocks
# parse from it, instead of silently returning an empty list (which
# looked like false "all done" success in the resume-aware batch
# script).
# ============================================================

import socket

_original_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

import os
import re
import sys
import json
import time
import wave
import threading
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
#----updated file path so it will read better ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NARRATION_FILE = os.path.join(BASE_DIR, "audio_scenes.txt")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "generated_audio")
MANIFEST_FILE = os.path.join(BASE_DIR, "manifest.json")

SECONDS_BETWEEN_REQUESTS = 3   # per key
MAX_RETRIES = 3

MODEL = "gemini-3.1-flash-tts-preview"
SAMPLE_RATE = 24000

NARRATION_FILE = "audio_scenes.txt"
OUTPUT_FOLDER = "generated_audio"
MANIFEST_FILE = "manifest.json"

AVAILABLE_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Enceladus",
]
DEFAULT_VOICE = "Zephyr"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def _collect_keys(prefix):
    found = {}
    for name, value in os.environ.items():
        match = re.match(rf"^{prefix}(\d+)$", name)
        if match and value:
            found[int(match.group(1))] = value
    return {f"AUD{n}": found[n] for n in sorted(found)}


GEMINI_KEYS = _collect_keys("GEMINI_KEY_")

if not GEMINI_KEYS:
    print("❌ No Gemini keys found. Add GEMINI_KEY_1=... to your .env file")
    sys.exit(1)

print(f"🔑 Loaded {len(GEMINI_KEYS)} Gemini key(s) for audio: {', '.join(GEMINI_KEYS)}")
if "GEMINI_KEY_1" in os.environ:
    print("   ⚠️  GEMINI_KEY_1 is also used by brain_core.py — no, wait, brain_core now uses")
    print("      OPENROUTER_KEY_N instead, so this collision no longer applies.")


_lane_last_time = {lane: 0 for lane in GEMINI_KEYS}
_lane_locks = {lane: threading.Lock() for lane in GEMINI_KEYS}

def rate_limit_lane(lane):
    with _lane_locks[lane]:
        wait = SECONDS_BETWEEN_REQUESTS - (time.time() - _lane_last_time[lane])
        if wait > 0:
            time.sleep(wait)
        _lane_last_time[lane] = time.time()


def classify_error(error_text):
    text = (error_text or "").lower()
    if any(s in text for s in ["rate limit", "429", "quota", "resource_exhausted"]):
        return "rate_limited"
    if any(s in text for s in ["network error", "timeout", "connection reset", "connection aborted",
                                "http 500", "http 502", "http 503", "http 504", "unavailable"]):
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


def load_narration():
    if not os.path.exists(NARRATION_FILE):
        print(f"❌ {NARRATION_FILE} not found")
        sys.exit(1)

    with open(NARRATION_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    entries = []
    for block in content.split("SCENE:"):
        block = block.strip()
        if not block:
            continue

        lines = block.splitlines()
        scene_key = lines[0].strip()
        voice = DEFAULT_VOICE
        text_lines = []

        for line in lines[1:]:
            if line.strip().upper().startswith("VOICE:"):
                requested_voice = line.split(":", 1)[1].strip()
                voice = requested_voice if requested_voice in AVAILABLE_VOICES else DEFAULT_VOICE
            else:
                text_lines.append(line)

        text = "\n".join(text_lines).strip()
        if scene_key and text:
            entries.append((scene_key, voice, text))

    # NEW: hard error on zero parsed entries instead of silent empty
    # return — this was the exact bug that made "audio never ran"
    # look like a false "all done, nothing to do" success.
    if not entries:
        print(f"❌ {NARRATION_FILE} exists but no valid SCENE: blocks were parsed from it.")
        print(f"   Check the format — each block needs 'SCENE:' followed by a scene key")
        print(f"   line (e.g. scene_001) and at least one line of narration text.")
        sys.exit(1)

    return entries


def save_wav(path, pcm_bytes, sample_rate=SAMPLE_RATE):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


def generate_speech(text, voice, api_key):
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=MODEL,
        contents=f"Read the following text aloud exactly as written, natural pacing:\n{text}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )

    part = response.candidates[0].content.parts[0]
    if not part.inline_data or not part.inline_data.data:
        raise RuntimeError("No audio returned")
    return part.inline_data.data


def generate_audio_once(lane, scene_key, voice, text):
    rate_limit_lane(lane)
    api_key = GEMINI_KEYS[lane]

    pcm_audio = generate_speech(text, voice, api_key)

    output_path = os.path.join(OUTPUT_FOLDER, f"{scene_key}.wav")
    save_wav(output_path, pcm_audio)
    return output_path