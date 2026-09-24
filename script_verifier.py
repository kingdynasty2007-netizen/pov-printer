# ============================================================
# FILE: script_verifier.py
# PURPOSE: Format-aware script quality gate.
#
# Returns structured JSON — never just "good/bad".
# Supports BIBLE_STORY format with full biblical accuracy checks.
# Uses the existing bible_strory_lookup.py for scripture retrieval.
# Uses the existing script_engine LLM infrastructure for AI checks.
#
# States: PASS | NEEDS_REWRITE | FAILED_QUALITY_GATE
#
# A script MUST NOT continue downstream unless passed == True.
#
# FIXES APPLIED:
#   - MAX_VERIFY_TOKENS raised to 4000 (prevents JSON truncation
#     on 15+ scene scripts).
#   - max_tokens explicitly cast to int() (Agnes Go backend rejects
#     floats in that field — HTTP 500).
#   - JSONDecodeError (truncated response) → NEEDS_REWRITE, not
#     FAILED_QUALITY_GATE.
#   - HTTP 500 / network errors → NEEDS_REWRITE with _server_error
#     flag so production_manager does not burn a rewrite slot.
#   - Unexpected errors remain FAILED_QUALITY_GATE (hard stop).
# ============================================================

import os
import re
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# CONFIG
# ============================================================

VERIFY_LLM_TIMEOUT = 180
MAX_VERIFY_LLM_RETRIES = 3
MAX_VERIFY_TOKENS = 4000   # must be int — Agnes backend is strict

# How many checks must pass for PASS status (80%)
PASS_THRESHOLD_RATIO = 0.80

# ============================================================
# CHECK DEFINITIONS
# ============================================================

BIBLE_CHECKS = [
    "hook",
    "biblical_accuracy",
    "scripture_usage",
    "story_structure",
    "pacing",
    "character_development",
    "emotional_progression",
    "curiosity",
    "climax",
    "payoff_resolution",
    "narration_quality",
    "visual_storytelling",
    "duration_scene_suitability",
]

GENERIC_CHECKS = [
    "hook",
    "story_structure",
    "pacing",
    "character_development",
    "emotional_progression",
    "climax",
    "payoff_resolution",
    "narration_quality",
    "visual_storytelling",
    "duration_scene_suitability",
]

# If ANY of these fail → FAILED_QUALITY_GATE regardless of overall ratio
BIBLE_CRITICAL_CHECKS = {"biblical_accuracy"}
GENERIC_CRITICAL_CHECKS = set()


# ============================================================
# LLM PROVIDER (reuses script_engine provider settings)
# ============================================================

def _get_llm_config():
    """Mirror script_engine provider config without importing the whole module."""
    provider = os.environ.get("SCRIPT_PROVIDER", "openrouter").strip().lower()
    if provider == "agnes":
        return {
            "url": "https://apihub.agnes-ai.com/v1/chat/completions",
            "model": os.environ.get("AGNES_TEXT_MODEL", "agnes-2.5-flash"),
            "key_prefix": "AGNES_TEXT_KEY_",
            "extra_headers": {},
            "disable_reasoning": False,
        }
    return {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": os.environ.get("SCRIPT_MODEL", "nvidia/nemotron-3.5-lightning:free"),
        "key_prefix": "SCRIPT_OPENROUTER_KEY_",
        "extra_headers": {
            "HTTP-Referer": "https://pov-printer.local",
            "X-Title": "POV Printer Script Verifier",
        },
        "disable_reasoning": True,
    }


def _collect_keys(prefix):
    found = {}
    for name, value in os.environ.items():
        match = re.match(rf"^{prefix}(\d+)$", name)
        if match and value:
            found[int(match.group(1))] = value
    return {f"K{n}": found[n] for n in sorted(found)}


def _call_llm(prompt):
    """
    Call the configured LLM. Returns raw text or raises RuntimeError.

    max_tokens is always sent as int — Agnes's Go backend will HTTP 500
    if it receives a float (json.unmarshal into uint fails).
    """
    cfg = _get_llm_config()
    keys = _collect_keys(cfg["key_prefix"])
    if not keys:
        raise RuntimeError(
            f"No LLM keys found for prefix '{cfg['key_prefix']}' — "
            "cannot run script verification."
        )

    key_list = list(keys.values())
    last_error = "unknown"

    for attempt in range(MAX_VERIFY_LLM_RETRIES):
        key = key_list[attempt % len(key_list)]
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        headers.update(cfg["extra_headers"])

        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": int(MAX_VERIFY_TOKENS),   # explicit int — Agnes strict
        }
        if cfg["disable_reasoning"]:
            payload["reasoning"] = {"effort": "none"}

        try:
            response = requests.post(
                cfg["url"], headers=headers, json=payload,
                timeout=VERIFY_LLM_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            last_error = f"network error: {e}"
            time.sleep(2)
            continue

        if response.status_code == 429:
            last_error = "rate limited"
            time.sleep(5)
            continue

        if response.status_code in (500, 502, 503):
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            time.sleep(3)
            continue

        if not response.ok:
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            continue

        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            last_error = "unexpected response shape"
            continue

    raise RuntimeError(
        f"LLM call failed after {MAX_VERIFY_LLM_RETRIES} attempts: {last_error}"
    )


# ============================================================
# JSON PARSING
# ============================================================

def _parse_json_response(raw):
    """Extract JSON from LLM response, stripping markdown fences."""
    raw = raw.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start:end + 1]
    return json.loads(raw)


# ============================================================
# BIBLE RESEARCH HELPER
# ============================================================

def retrieve_bible_research(topic):
    """
    Use bible_strory_lookup.py to retrieve relevant scripture.
    Returns (story_facts dict, scripture_references list).
    Gracefully degrades if Supabase is unavailable.
    """
    story_facts = {
        "topic": topic,
        "characters": [],
        "events": [],
        "scripture_passages": [],
    }
    scripture_references = []

    try:
        from bible_strory_lookup import search_verses

        keywords = [w for w in topic.split() if len(w) > 3]

        for keyword in keywords[:3]:
            try:
                verses = search_verses(keyword, limit=5)
                for v in verses:
                    ref = (
                        f"{v.get('book', '')} "
                        f"{v.get('chapter', '')}:{v.get('verse', '')}"
                    )
                    text = v.get("text", v.get("verse_text", ""))
                    if ref.strip() and text:
                        entry = {"reference": ref.strip(), "text": text}
                        if entry not in scripture_references:
                            scripture_references.append(entry)
            except Exception:
                pass

        story_facts["scripture_passages"] = scripture_references
        story_facts["source"] = "bible_database"

    except ImportError:
        story_facts["source"] = "bible_database_unavailable"
    except Exception as e:
        story_facts["source"] = f"bible_database_error: {str(e)[:100]}"

    return story_facts, scripture_references


# ============================================================
# PROMPT BUILDERS
# ============================================================

def _build_bible_verify_prompt(script_text, topic, scene_count,
                                duration_secs, scripture_references):
    if scripture_references:
        lines = [
            f"  - {r['reference']}: {r['text'][:120]}"
            for r in scripture_references[:10]
        ]
        scripture_block = (
            "RETRIEVED SCRIPTURE (use to check accuracy):\n" + "\n".join(lines)
        )
    else:
        scripture_block = (
            "RETRIEVED SCRIPTURE: None retrieved — use general biblical knowledge."
        )

    checks_desc = """
Evaluate each check and respond with true/false + a specific reason.

CHECKS:
1.  hook: Does the opening immediately create interest, tension, or a compelling question?
2.  biblical_accuracy: Are major events, characters, relationships, and outcomes faithful
    to scripture? Flag invented facts presented as biblical truth, wrong attributions,
    or contradictions.
3.  scripture_usage: Are relevant scripture passages referenced naturally and correctly?
4.  story_structure: Clear hook → setup → conflict → escalation → climax → resolution?
5.  pacing: Does every scene move the story forward? No repeated exposition?
6.  character_development: Is the main character's motivation and arc clear?
7.  emotional_progression: Does emotional intensity develop naturally toward the climax?
8.  curiosity: Does each section give the viewer a reason to keep watching?
9.  climax: Does the story build toward a meaningful, clearly defined major event?
10. payoff_resolution: Does the ending properly resolve the story?
11. narration_quality: Is the language clear, warm, cinematic, and appropriate for a
    general audience?
12. visual_storytelling: Can each scene be clearly represented through animation?
13. duration_scene_suitability: Is the content appropriate for the number of scenes
    and duration?
"""

    return f"""You are a Bible story script quality evaluator for an animated video pipeline.

TOPIC: {topic}
DURATION: {duration_secs} seconds ({scene_count} scenes at 10 seconds each)

{scripture_block}

SCRIPT TO EVALUATE:
---
{script_text[:6000]}
---

{checks_desc}

IMPORTANT BIBLICAL ACCURACY RULES:
- Invented dialogue is ALLOWED if it does not contradict scripture and is not presented
  as an exact quotation.
- Reasonable storytelling context is ALLOWED if clearly treated as dramatization.
- Invented events presented as biblical fact = FAIL biblical_accuracy.
- Wrong character relationships or outcomes = FAIL biblical_accuracy.
- Speculation presented as confirmed fact = FAIL biblical_accuracy.

Respond ONLY with valid JSON in this exact structure (no extra text, no markdown fences):
{{
  "passed": true,
  "status": "PASS",
  "checks": {{
    "hook": {{"passed": true, "reason": "specific reason"}},
    "biblical_accuracy": {{"passed": true, "reason": "specific reason"}},
    "scripture_usage": {{"passed": true, "reason": "specific reason"}},
    "story_structure": {{"passed": true, "reason": "specific reason"}},
    "pacing": {{"passed": true, "reason": "specific reason"}},
    "character_development": {{"passed": true, "reason": "specific reason"}},
    "emotional_progression": {{"passed": true, "reason": "specific reason"}},
    "curiosity": {{"passed": true, "reason": "specific reason"}},
    "climax": {{"passed": true, "reason": "specific reason"}},
    "payoff_resolution": {{"passed": true, "reason": "specific reason"}},
    "narration_quality": {{"passed": true, "reason": "specific reason"}},
    "visual_storytelling": {{"passed": true, "reason": "specific reason"}},
    "duration_scene_suitability": {{"passed": true, "reason": "specific reason"}}
  }},
  "required_fixes": [],
  "warnings": []
}}

Rules for the status field:
- "PASS": passed == true (all critical checks pass AND >= 80% of all checks pass)
- "NEEDS_REWRITE": some checks failed but the script is salvageable
- "FAILED_QUALITY_GATE": biblical_accuracy failed OR too many checks failed to salvage
"""


def _build_generic_verify_prompt(script_text, topic, scene_count,
                                  duration_secs, fmt):
    return f"""You are a script quality evaluator for a short-form video pipeline.

FORMAT: {fmt}
TOPIC: {topic}
DURATION: {duration_secs} seconds ({scene_count} scenes at 10 seconds each)

SCRIPT TO EVALUATE:
---
{script_text[:6000]}
---

Evaluate each check and respond with true/false + a specific reason.

CHECKS:
1.  hook: Does the opening immediately create interest or a compelling question?
2.  story_structure: Clear beginning → conflict → climax → resolution?
3.  pacing: Does every scene move the story forward? No repeated exposition?
4.  character_development: Is the main character's motivation and arc clear?
5.  emotional_progression: Does emotional intensity develop naturally?
6.  climax: Does the story build toward a meaningful major event?
7.  payoff_resolution: Does the ending properly resolve the story?
8.  narration_quality: Is the language clear and appropriate for the audience?
9.  visual_storytelling: Can each scene be clearly represented through animation?
10. duration_scene_suitability: Is the content appropriate for the number of scenes?

Respond ONLY with valid JSON in this exact structure (no extra text, no markdown fences):
{{
  "passed": true,
  "status": "PASS",
  "checks": {{
    "hook": {{"passed": true, "reason": "specific reason"}},
    "story_structure": {{"passed": true, "reason": "specific reason"}},
    "pacing": {{"passed": true, "reason": "specific reason"}},
    "character_development": {{"passed": true, "reason": "specific reason"}},
    "emotional_progression": {{"passed": true, "reason": "specific reason"}},
    "climax": {{"passed": true, "reason": "specific reason"}},
    "payoff_resolution": {{"passed": true, "reason": "specific reason"}},
    "narration_quality": {{"passed": true, "reason": "specific reason"}},
    "visual_storytelling": {{"passed": true, "reason": "specific reason"}},
    "duration_scene_suitability": {{"passed": true, "reason": "specific reason"}}
  }},
  "required_fixes": [],
  "warnings": []
}}

Rules for the status field:
- "PASS": passed == true (>= 80% of checks pass)
- "NEEDS_REWRITE": some checks failed but salvageable
- "FAILED_QUALITY_GATE": too many checks failed to salvage
"""


# ============================================================
# STATUS RESOLUTION
# ============================================================
def _resolve_status(checks_result, fmt, critical_checks):
    """
    Derive final (passed, status) from check results.
    Does NOT trust the LLM's own status field — recomputes it.
    """
    check_values = {k: v.get("passed", False) for k, v in checks_result.items()}

    # Critical check failure → hard gate regardless of ratio
    for critical in critical_checks:
        if critical in check_values and not check_values[critical]:
            return False, "FAILED_QUALITY_GATE"

    total = len(check_values)
    if total == 0:
        return False, "FAILED_QUALITY_GATE"

    passed_count = sum(1 for v in check_values.values() if v)
    ratio = passed_count / total

    if ratio >= PASS_THRESHOLD_RATIO:
        return True, "PASS"
    elif ratio >= 0.5:
        return False, "NEEDS_REWRITE"
    else:
        return False, "FAILED_QUALITY_GATE"



# ============================================================
# PUBLIC API
# ============================================================

def verify(script_text, topic, fmt, duration_secs, scene_count,
           scripture_references=None):
    """
    Verify a script. Returns a structured result dict.

    Args:
        script_text:          Full script text to verify.
        topic:                Topic / title of the story.
        fmt:                  Format name (e.g. 'bible_story').
        duration_secs:        Target duration in seconds.
        scene_count:          Number of scenes.
        scripture_references: Optional list of retrieved scripture dicts.

    Returns dict with keys:
        passed, status, checks, required_fixes, warnings
        Optional: error, _parse_error, _server_error
    """
    is_bible = fmt.lower() in ("bible_story", "bible story")

    if is_bible:
        prompt = _build_bible_verify_prompt(
            script_text, topic, scene_count, duration_secs,
            scripture_references or [],
        )
        critical_checks = BIBLE_CRITICAL_CHECKS
    else:
        prompt = _build_generic_verify_prompt(
            script_text, topic, scene_count, duration_secs, fmt,
        )
        critical_checks = GENERIC_CRITICAL_CHECKS

    # ---- Call LLM ----
    try:
        raw = _call_llm(prompt)

    except RuntimeError as e:
        # LLM call failed (HTTP 500, network, rate limit, etc.)
        # Server errors are NOT script quality failures — treat as retryable.
        error_text = str(e)
        is_server_error = any(x in error_text for x in [
            "HTTP 500", "HTTP 502", "HTTP 503",
            "network error", "rate limit", "rate limited",
        ])
        return {
            "passed": False,
            "status": "NEEDS_REWRITE" if is_server_error else "FAILED_QUALITY_GATE",
            "checks": {},
            "required_fixes": [
                (
                    "Verification service temporarily unavailable — not a script quality "
                    "failure. Re-running will retry verification."
                ) if is_server_error else (
                    f"Verification system error: {error_text[:200]}"
                )
            ],
            "warnings": (
                [f"Server error (will retry): {error_text[:200]}"]
                if is_server_error else []
            ),
            "error": error_text,
            "_server_error": is_server_error,
        }

    except Exception as e:
        # Unexpected error — hard stop.
        return {
            "passed": False,
            "status": "FAILED_QUALITY_GATE",
            "checks": {},
            "required_fixes": [f"Unexpected verification error: {str(e)[:200]}"],
            "warnings": [],
            "error": str(e),
        }

    # ---- Parse JSON ----
    try:
        result = _parse_json_response(raw)

    except json.JSONDecodeError as e:
        # Truncated / malformed JSON from LLM.
        # This happens when max_tokens cuts the response mid-string.
        # The script itself may be fine — treat as retryable, not a hard gate.
        return {
            "passed": False,
            "status": "NEEDS_REWRITE",
            "checks": {},
            "required_fixes": [
                "Verification response was truncated (JSON parse error). "
                "This is a system issue, not a script quality failure. "
                "Re-running will retry verification."
            ],
            "warnings": [f"JSON parse error: {str(e)[:200]}"],
            "error": str(e),
            "_parse_error": True,
        }

    # ---- Validate and normalise ----
    checks = result.get("checks", {})
    if not isinstance(checks, dict):
        checks = {}

    # Recompute status from actual check values — don't blindly trust the LLM
    passed, status = _resolve_status(checks, fmt, critical_checks)

    return {
        "passed": passed,
        "status": status,
        "checks": checks,
        "required_fixes": result.get("required_fixes", []),
        "warnings": result.get("warnings", []),
    }


# ============================================================
# DRY-RUN VERIFIER (no LLM calls — for safe test mode)
# ============================================================

def verify_dry_run(script_text, topic, fmt, duration_secs, scene_count,
                   force_pass=False):
    """
    Dry-run verifier — no LLM calls, no API spend.
    Returns NEEDS_REWRITE on first call, PASS on subsequent calls
    (controlled by force_pass flag).
    Used by: python manual_runner.py --dry-run
    """
    check_names = (
        BIBLE_CHECKS
        if fmt.lower() in ("bible_story", "bible story")
        else GENERIC_CHECKS
    )

    if force_pass:
        checks = {
            name: {"passed": True, "reason": "[dry-run] auto-passed"}
            for name in check_names
        }
        return {
            "passed": True,
            "status": "PASS",
            "checks": checks,
            "required_fixes": [],
            "warnings": ["[DRY RUN] No real verification performed."],
        }
    else:
        checks = {}
        for name in check_names:
            if name in ("hook", "pacing"):
                checks[name] = {
                    "passed": False,
                    "reason": "[dry-run] simulated failure",
                }
            else:
                checks[name] = {
                    "passed": True,
                    "reason": "[dry-run] auto-passed",
                }
        return {
            "passed": False,
            "status": "NEEDS_REWRITE",
            "checks": checks,
            "required_fixes": [
                "[DRY RUN] Strengthen the hook.",
                "[DRY RUN] Remove repeated exposition.",
            ],
            "warnings": ["[DRY RUN] No real verification performed."],
        }
