# ============================================================
# FILE: brain_core.py
# PURPOSE: AI-driven prompt repair via OpenRouter. Multi-key
# rotation with a GLOBAL throttle, checklist-guided diagnosis,
# sanity check on its own output.
# ============================================================

import socket

_original_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

import os
import re
import json
import time
import itertools
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_MODEL = "google/gemma-4-31b-it:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

GLOBAL_SECONDS_BETWEEN_BRAIN_CALLS = 4
MAX_BRAIN_CALL_ATTEMPTS = 3
REWRITE_AFTER_ATTEMPTS = 3

KNOWN_CATEGORIES = [
    "content_policy", "character_mismatch", "role_reversal",
    "extra_person", "proportion_drift", "style_drift", "era_inconsistency", "other",
]


def _collect_keys(prefix):
    found = {}
    for name, value in os.environ.items():
        match = re.match(rf"^{prefix}(\d+)$", name)
        if match and value:
            found[int(match.group(1))] = value
    return {f"OR{n}": found[n] for n in sorted(found)}

OPENROUTER_KEYS = _collect_keys("OPENROUTER_KEY_")
BRAIN_AVAILABLE = bool(OPENROUTER_KEYS)

if not BRAIN_AVAILABLE:
    print("⚠️ brain_core: no OPENROUTER_KEY_N found — smart retry disabled.")
else:
    print(f"🧠 brain_core: {len(OPENROUTER_KEYS)} key(s) loaded: {', '.join(OPENROUTER_KEYS)}")

_global_lock = threading.Lock()
_global_last_call_time = 0
_global_cooldown_until = 0

def rate_limit_global():
    global _global_last_call_time
    with _global_lock:
        now = time.time()
        wait_cooldown = _global_cooldown_until - now
        if wait_cooldown > 0:
            time.sleep(wait_cooldown)
            now = time.time()
        wait_pacing = GLOBAL_SECONDS_BETWEEN_BRAIN_CALLS - (now - _global_last_call_time)
        if wait_pacing > 0:
            time.sleep(wait_pacing)
        _global_last_call_time = time.time()

def trigger_global_cooldown(seconds):
    global _global_cooldown_until
    with _global_lock:
        candidate = time.time() + seconds
        if candidate > _global_cooldown_until:
            _global_cooldown_until = candidate

_key_cycle = itertools.cycle(OPENROUTER_KEYS.keys()) if OPENROUTER_KEYS else None
_key_cycle_lock = threading.Lock()

def _next_lane():
    with _key_cycle_lock:
        return next(_key_cycle)


def _is_low_quality(fix, issue):
    if fix["strategy"] == "patch":
        text = fix.get("extra_instruction", "")
    else:
        text = fix.get("scene_text", "")
    if len(text.strip()) < 15:
        return True
    generic_phrases = ["make sure", "please fix", "ensure correct", "fix the issue"]
    if text.strip().lower() in generic_phrases:
        return True
    return False


def get_fix(scene_key, characters, scene_text, issue, attempt_number,
            previous_extra_instruction="", on_attempt=None):
    if not BRAIN_AVAILABLE:
        return None, "no_openrouter_key"

    character_list = ", ".join(c.replace("_", " ") for c in characters)
    force_rewrite = attempt_number >= REWRITE_AFTER_ATTEMPTS

    repair_prompt = f"""
Repair assistant for an AI image pipeline. Diagnose this failure using
the checklist below, then produce ONE fix.

Scene ID: {scene_key}
Characters: {character_list}
Original scene text: {scene_text}
Previous patch (if any): {previous_extra_instruction or "none"}
Failure reason: {issue}
Escalation attempt: {attempt_number}

STEP 1 — Classify (pick exactly one): {", ".join(KNOWN_CATEGORIES)}

STEP 2 — Decide fix:
- Attempt 1-2: PATCH — one short, SPECIFIC instruction that directly
  addresses the exact reported issue. Not generic advice.
- Attempt 3+ OR repeating category: REWRITE — rewrite scene_text
  (action/framing/blocking only). Never add appearance/clothing
  details — those belong only in the reference image.
{"A rewrite is required this time." if force_rewrite else ""}

Respond ONLY with JSON:
{{"category": "...", "strategy": "patch" or "rewrite",
"extra_instruction": "<specific fix if patch, else empty>",
"scene_text": "<rewritten text if rewrite, else empty>"}}
"""

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": repair_prompt}],
        "reasoning": {"enabled": True},
    }

    last_error = "unknown"
    for call_attempt in range(1, MAX_BRAIN_CALL_ATTEMPTS + 1):
        lane = _next_lane()
        if on_attempt:
            on_attempt(call_attempt, MAX_BRAIN_CALL_ATTEMPTS, lane)

        rate_limit_global()
        headers = {"Authorization": f"Bearer {OPENROUTER_KEYS[lane]}", "Content-Type": "application/json"}

        try:
            response = requests.post(OPENROUTER_URL, headers=headers, data=json.dumps(payload), timeout=60)
        except requests.exceptions.RequestException:
            last_error = f"[{lane}] network_error"
            continue

        if response.status_code == 429:
            last_error = f"[{lane}] rate_limited"
            trigger_global_cooldown(30)
            continue

        if not response.ok:
            last_error = f"[{lane}] http_{response.status_code}"
            continue

        try:
            message = response.json()["choices"][0]["message"]
            raw = (message.get("content") or "").strip()
            raw = raw.strip("```json").strip("```").strip()
            if not raw:
                raise ValueError("empty")
            parsed = json.loads(raw)
        except Exception:
            last_error = f"[{lane}] parse_error"
            continue

        strategy = parsed.get("strategy")
        category = parsed.get("category", "other")

        if strategy == "patch" and parsed.get("extra_instruction"):
            fix = {"strategy": "patch", "extra_instruction": parsed["extra_instruction"], "category": category}
        elif strategy == "rewrite" and parsed.get("scene_text"):
            fix = {"strategy": "rewrite", "scene_text": parsed["scene_text"], "category": category}
        else:
            last_error = f"[{lane}] malformed_response"
            continue

        if _is_low_quality(fix, issue):
            last_error = f"[{lane}] low_quality_response"
            continue

        return fix, None

    return None, f"failed after {MAX_BRAIN_CALL_ATTEMPTS} attempts: {last_error}"