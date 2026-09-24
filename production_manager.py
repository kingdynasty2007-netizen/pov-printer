# ============================================================
# FILE: production_manager.py
# PURPOSE: Orchestrates the full manual production pipeline.
#
# FLOW:
#   INPUT → RESEARCH → SCRIPT (v001) → VERIFY → REWRITE LOOP
#   → HARD PASS GATE → BREAKDOWN → METADATA → THUMBNAIL
#   → (existing media systems)
#
# Does NOT run image/audio/video generation itself.
# Calls the existing script_engine, bible_strory_lookup,
# ref_character_generator, batch_* generators via subprocess
# (same pattern as topic_queue.py).
#
# YouTube API: NOT implemented here.
# Automatic queue: NOT implemented here.
#
# FIXES APPLIED:
#   - Server/parse errors from verifier do not burn rewrite slots.
#   - Scene count sanity check: truncated scripts (< 80% of expected
#     scenes) override FAILED_QUALITY_GATE to NEEDS_REWRITE.
#   - scenes.txt / audio_scenes.txt / video_scenes.txt are only
#     written to root AFTER script PASS — not during generation.
#   - video_scenes.txt added to root sync.
# ============================================================

import os
import sys
import json
import re
import time
import subprocess
from datetime import datetime, timezone
from dotenv import load_dotenv

import storage_manager as sm
import script_verifier as sv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

MAX_REWRITE_ATTEMPTS = 3
SECONDS_PER_SCENE = 10   # must match script_engine.SECONDS_PER_SCENE


# ============================================================
# HELPERS
# ============================================================

def _scene_count(duration_secs):
    return max(1, duration_secs // SECONDS_PER_SCENE)


def _log(run_id, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{run_id}] {ts}  {msg}")


def _count_scenes_in_script(script_text):
    """Count scene blocks by --- separators in the SCENES section."""
    scenes_section = script_text
    if "=== SCENES ===" in script_text:
        parts = script_text.split("=== SCENES ===", 1)
        if len(parts) > 1:
            rest = parts[1]
            if "=== AUDIO ===" in rest:
                scenes_section = rest.split("=== AUDIO ===")[0]
            else:
                scenes_section = rest
    blocks = [b.strip() for b in scenes_section.split("---") if b.strip()]
    return max(len(blocks), 1)


# ============================================================
# RESEARCH
# ============================================================

def run_research(run_id, paths, topic, fmt):
    """
    Retrieve Bible research (or generic research placeholder).
    Saves story_facts.json and scripture_references.json.
    Returns (story_facts, scripture_references).
    """
    _log(run_id, "📖 Running research...")

    is_bible = fmt.lower() in ("bible_story", "bible story")

    if is_bible:
        story_facts, scripture_references = sv.retrieve_bible_research(topic)
    else:
        story_facts = {"topic": topic, "format": fmt, "source": "generic"}
        scripture_references = []

    sm.save_json(paths, "research", "story_facts.json", story_facts)
    sm.save_json(paths, "research", "scripture_references.json", scripture_references)

    _log(run_id, f"   ✅ Research saved ({len(scripture_references)} scripture reference(s))")
    return story_facts, scripture_references


# ============================================================
# SCRIPT GENERATION
# ============================================================

def generate_script(run_id, paths, topic, fmt, duration_secs,
                    character_names, raw_script_text=None,
                    dry_run=False):
    """
    Generate or ingest a script. Saves as the next version (v001 on first run).
    Returns (script_text, version_label).

    NOTE: script_engine.run() writes scenes.txt / audio_scenes.txt to the
    project root as a side effect. Those root files are STAGING ONLY and
    will be overwritten after PASS by _sync_breakdown_to_root(). Do not
    treat them as approved until the pipeline reaches breakdown.
    """
    scene_count = _scene_count(duration_secs)

    if raw_script_text:
        _log(run_id, "📄 Ingesting existing script as v001...")
        script_text = raw_script_text

    elif dry_run:
        _log(run_id, "🧪 [DRY RUN] Generating placeholder script...")
        script_text = _dry_run_script(topic, scene_count)

    else:
        _log(run_id, f"✍️  Generating script ({scene_count} scenes)...")
        import script_engine
        result = script_engine.run(
            topic=topic,
            style=fmt,
            duration_secs=duration_secs,
            scene_count=scene_count,
            character_names=character_names,
            topic_id=None,
            raw_script_text=None,
        )
        scenes_txt = result.get("scenes_txt", "")
        audio_txt = result.get("audio_txt", "")
        script_text = f"=== SCENES ===\n{scenes_txt}\n\n=== AUDIO ===\n{audio_txt}"

    version_label = sm.save_script_version(paths, script_text)
    sm.update_production(run_id,
                         current_script_version=version_label,
                         script_status="generated")
    return script_text, version_label


def _dry_run_script(topic, scene_count):
    """Minimal placeholder script for dry-run / test mode."""
    lines = [f"[DRY RUN SCRIPT] Topic: {topic}\n"]
    for i in range(1, scene_count + 1):
        lines.append(f"SCENE {i:03d}: Placeholder scene {i} for {topic}.")
        if i < scene_count:
            lines.append("---")
    return "\n".join(lines)


# ============================================================
# REWRITE
# ============================================================

def rewrite_script(run_id, paths, topic, fmt, duration_secs,
                   character_names, previous_script_text,
                   verification_result, dry_run=False):
    """
    Generate a new script version based on verification failures.
    Returns (new_script_text, new_version_label).
    """
    scene_count = _scene_count(duration_secs)
    required_fixes = verification_result.get("required_fixes", [])
    fixes_text = (
        "\n".join(f"- {f}" for f in required_fixes)
        if required_fixes
        else "- General quality improvement needed."
    )

    if dry_run:
        _log(run_id, "🧪 [DRY RUN] Generating rewrite placeholder...")
        new_script_text = _dry_run_script(f"{topic} [REWRITE]", scene_count)

    else:
        _log(run_id, "🔄 Rewriting script based on verification feedback...")
        import script_engine

        rewrite_topic = (
            f"REWRITE INSTRUCTIONS — the previous version of this script failed "
            f"quality checks.\n"
            f"Required fixes:\n{fixes_text}\n\n"
            f"Previous script (for reference — do NOT copy it, fix the issues):\n"
            f"{previous_script_text[:3000]}\n\n"
            f"Now write an improved version of the script for topic: {topic}\n"
        )

        result = script_engine.run(
            topic=rewrite_topic,
            style=fmt,
            duration_secs=duration_secs,
            scene_count=scene_count,
            character_names=character_names,
            topic_id=None,
            raw_script_text=None,
        )
        scenes_txt = result.get("scenes_txt", "")
        audio_txt = result.get("audio_txt", "")
        new_script_text = f"=== SCENES ===\n{scenes_txt}\n\n=== AUDIO ===\n{audio_txt}"

    new_version_label = sm.save_script_version(paths, new_script_text)
    sm.update_production(run_id,
                         current_script_version=new_version_label,
                         script_status="rewritten")
    _log(run_id, f"   ✅ Rewrite saved as {new_version_label}")
    return new_script_text, new_version_label


# ============================================================
# VERIFICATION LOOP
# ============================================================

def run_verification_loop(run_id, paths, topic, fmt, duration_secs,
                           character_names, initial_script_text,
                           initial_version_label, scripture_references,
                           dry_run=False):
    """
    Verify → rewrite → re-verify loop.
    Enforces MAX_REWRITE_ATTEMPTS hard limit.

    Server errors and parse errors do NOT burn a rewrite slot —
    they retry verification on the same script version.

    Truncated scripts (< 80% of expected scenes) override
    FAILED_QUALITY_GATE to NEEDS_REWRITE automatically.

    Returns (approved_script_text, approved_version_label, final_verification_result)
    or raises RuntimeError if FAILED_QUALITY_GATE.
    """
    scene_count = _scene_count(duration_secs)
    current_script = initial_script_text
    current_version = initial_version_label
    rewrite_count = 0

    while True:
        _log(run_id, f"🔍 Verifying {current_version}...")
        sm.update_production(run_id,
                             stage="verifying",
                             verification_status="running")

        # ---- Run verifier ----
        if dry_run:
            force_pass = (rewrite_count >= 1)
            result = sv.verify_dry_run(
                current_script, topic, fmt, duration_secs, scene_count,
                force_pass=force_pass,
            )
        else:
            result = sv.verify(
                current_script, topic, fmt, duration_secs, scene_count,
                scripture_references=scripture_references,
            )

        sm.save_verification(paths, current_version, result)
        _log(run_id, f"   Status: {result['status']}")

        # ---- Scene count sanity check ----
        # If the script is simply missing scenes (generation was truncated),
        # override FAILED_QUALITY_GATE to NEEDS_REWRITE so the pipeline
        # regenerates rather than hard-stopping.
        expected_scenes = scene_count
        actual_scenes = _count_scenes_in_script(current_script)
        if (
            result["status"] == "FAILED_QUALITY_GATE"
            and actual_scenes < expected_scenes * 0.8
        ):
            _log(run_id,
                 f"   ⚠️  Script has {actual_scenes}/{expected_scenes} scenes — "
                 f"overriding FAILED_QUALITY_GATE to NEEDS_REWRITE "
                 f"(generation was truncated, not a quality failure)")
            result["status"] = "NEEDS_REWRITE"
            result["passed"] = False
            if not result.get("required_fixes"):
                result["required_fixes"] = [
                    f"Script was truncated — only {actual_scenes} of "
                    f"{expected_scenes} scenes were generated. "
                    f"Regenerate with the full scene count."
                ]

        # ---- PASS ----
        if result["passed"]:
            _log(run_id, f"   ✅ QUALITY GATE PASSED ({current_version})")
            sm.update_production(run_id,
                                 verification_status="passed",
                                 script_status="approved",
                                 stage="approved")
            sm.set_current_script(paths, current_script)
            return current_script, current_version, result

        # ---- FAILED_QUALITY_GATE — hard stop ----
        if result["status"] == "FAILED_QUALITY_GATE":
            sm.update_production(run_id,
                                 verification_status="failed_quality_gate",
                                 stage="failed",
                                 error="Script failed quality gate — pipeline stopped.")
            raise RuntimeError(
                f"❌ FAILED_QUALITY_GATE on {current_version}. "
                f"Required fixes: {result.get('required_fixes', [])}"
            )

        # ---- NEEDS_REWRITE ----
        # Server errors and parse errors do NOT burn a rewrite slot.
        is_system_error = (
            result.get("_parse_error", False)
            or result.get("_server_error", False)
        )

        if is_system_error:
            _log(run_id,
                 "   ⚠️  SYSTEM ERROR during verification — retrying same version "
                 "(rewrite slot NOT consumed)")
        else:
            rewrite_count += 1
            _log(run_id,
                 f"   ⚠️  NEEDS_REWRITE "
                 f"(attempt {rewrite_count}/{MAX_REWRITE_ATTEMPTS})")
            for fix in result.get("required_fixes", []):
                _log(run_id, f"      → {fix}")

        # ---- Rewrite limit reached ----
        if rewrite_count >= MAX_REWRITE_ATTEMPTS and not is_system_error:
            sm.update_production(run_id,
                                 verification_status="failed_quality_gate",
                                 stage="failed",
                                 error=f"Exceeded {MAX_REWRITE_ATTEMPTS} rewrite attempts.")
            raise RuntimeError(
                f"❌ Script still failing after {MAX_REWRITE_ATTEMPTS} rewrite "
                f"attempt(s). Pipeline stopped. "
                f"Last status: {result['status']}"
            )

        sm.update_production(run_id,
                             stage="rewriting",
                             verification_status="needs_rewrite")

        # System errors retry verification on the same script — no rewrite needed
        if not is_system_error:
            current_script, current_version = rewrite_script(
                run_id, paths, topic, fmt, duration_secs,
                character_names, current_script, result,
                dry_run=dry_run,
            )


# ============================================================
# POST-PASS BREAKDOWN
# ============================================================

def run_breakdown(run_id, paths, approved_script_text, topic, fmt,
                  duration_secs, character_names, story_facts,
                  scripture_references, dry_run=False):
    """
    After PASS: produce all breakdown artifacts.
    Saves to breakdown/ directory.
    Does NOT call image/audio/video generation.
    """
    _log(run_id, "📋 Running post-pass breakdown...")
    scene_count = _scene_count(duration_secs)

    # ---- Characters ----
    characters_data = _extract_characters(
        approved_script_text, character_names, fmt
    )
    sm.save_json(paths, "breakdown", "characters.json", characters_data)

    # ---- Scenes ----
    scenes_data = _extract_scenes(approved_script_text, scene_count, fmt)
    scenes_txt = _format_scenes_txt(scenes_data)
    sm.save_text(paths, "breakdown", "scenes.txt", scenes_txt)

    # ---- Audio scenes ----
    audio_txt = _format_audio_scenes(scenes_data)
    sm.save_text(paths, "breakdown", "audio_scenes.txt", audio_txt)

    # ---- Video scenes ----
    video_txt = _format_video_scenes(scenes_data)
    sm.save_text(paths, "breakdown", "video_scenes.txt", video_txt)

    # ---- Image prompts ----
    image_prompts = _build_image_prompts(scenes_data, fmt)
    sm.save_json(paths, "breakdown", "image_prompts.json", image_prompts)

    # ---- Research artifacts (ensure latest copy is in research/) ----
    sm.save_json(paths, "research", "story_facts.json", story_facts)
    sm.save_json(paths, "research", "scripture_references.json", scripture_references)

    _log(run_id, f"   ✅ Breakdown complete ({len(scenes_data)} scenes)")
    return scenes_data, characters_data


def _extract_characters(script_text, character_names, fmt):
    """Extract character list from script or use provided names."""
    found = set(c.lower() for c in character_names if c)

    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("CHARACTERS:"):
            names_part = stripped.split(":", 1)[1]
            for name in names_part.split(","):
                name = name.strip().lower()
                if name and name not in ("", "none"):
                    found.add(name)

    return {
        "characters": [
            {"name": name, "format": fmt, "needs_reference": True}
            for name in sorted(found)
        ]
    }


def _extract_scenes(script_text, scene_count, fmt):
    """
    Parse scenes from the approved script text.
    Returns list of scene dicts.
    """
    scenes = []

    # Extract SCENES section
    scenes_section = ""
    if "=== SCENES ===" in script_text:
        parts = script_text.split("=== SCENES ===", 1)
        if len(parts) > 1:
            rest = parts[1]
            scenes_section = (
                rest.split("=== AUDIO ===")[0]
                if "=== AUDIO ===" in rest
                else rest
            )
    else:
        scenes_section = script_text

    # Split on scene breaks
    raw_blocks = (
        [b.strip() for b in scenes_section.split("---") if b.strip()]
        if "---" in scenes_section
        else ([scenes_section.strip()] if scenes_section.strip() else [])
    )

    # Extract audio section
    audio_section = ""
    if "=== AUDIO ===" in script_text:
        audio_section = script_text.split("=== AUDIO ===", 1)[1]

    audio_blocks = _parse_audio_section(audio_section)

    for i, block in enumerate(raw_blocks, start=1):
        characters = []
        char_match = re.search(r"CHARACTERS:\s*(.+)", block, re.IGNORECASE)
        if char_match:
            characters = [
                c.strip().lower()
                for c in char_match.group(1).split(",")
                if c.strip()
            ]

        narration = audio_blocks[i - 1] if i - 1 < len(audio_blocks) else ""

        scenes.append({
            "scene_id": f"scene_{i:03d}",
            "scene_number": i,
            "duration_secs": 10,
            "characters": characters,
            "description": block,
            "narration": narration,
            "environment": _extract_environment(block),
            "emotion": _extract_emotion(block),
            "scripture_ref": _extract_scripture_ref(block),
        })

    # If no scenes parsed, create placeholders
    if not scenes:
        for i in range(1, scene_count + 1):
            scenes.append({
                "scene_id": f"scene_{i:03d}",
                "scene_number": i,
                "duration_secs": 10,
                "characters": [],
                "description": f"Scene {i}",
                "narration": "",
                "environment": "",
                "emotion": "",
                "scripture_ref": "",
            })

    return scenes


def _parse_audio_section(audio_text):
    """Extract narration lines from audio section."""
    narrations = []
    if not audio_text:
        return narrations
    for block in audio_text.split("SCENE:"):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        text_lines = []
        for line in lines[1:]:   # skip scene key line
            if line.strip().upper().startswith("VOICE:"):
                continue
            text_lines.append(line)
        narration = "\n".join(text_lines).strip()
        if narration:
            narrations.append(narration)
    return narrations


def _extract_environment(block):
    env_keywords = [
        "village", "desert", "temple", "palace", "field", "mountain",
        "river", "cave", "city", "wilderness", "courtyard", "road",
        "hillside", "valley", "shore", "garden", "prison", "well",
    ]
    block_lower = block.lower()
    found = [k for k in env_keywords if k in block_lower]
    return ", ".join(found) if found else "biblical-era setting"


def _extract_emotion(block):
    emotion_keywords = {
        "fear":    ["fear", "afraid", "terrified", "scared"],
        "hope":    ["hope", "hopeful", "faith"],
        "grief":   ["grief", "mourning", "weeping", "sad"],
        "courage": ["courage", "brave", "bold"],
        "joy":     ["joy", "rejoice", "celebrate"],
        "tension": ["tense", "danger", "threat", "urgent"],
        "awe":     ["awe", "wonder", "amazed"],
        "love":    ["love", "compassion", "mercy"],
        "anger":   ["anger", "wrath", "furious"],
    }
    block_lower = block.lower()
    for emotion, keywords in emotion_keywords.items():
        if any(k in block_lower for k in keywords):
            return emotion
    return "neutral"


def _extract_scripture_ref(block):
    pattern = r"\b(\d?\s?[A-Za-z]+)\s+(\d+):(\d+)(?:-(\d+))?\b"
    matches = re.findall(pattern, block)
    if matches:
        refs = []
        for m in matches[:2]:
            book, ch, v_start, v_end = m
            ref = f"{book.strip()} {ch}:{v_start}"
            if v_end:
                ref += f"-{v_end}"
            refs.append(ref)
        return "; ".join(refs)
    return ""


def _format_scenes_txt(scenes_data):
    """Format scenes.txt — compatible with existing image pipeline."""
    blocks = []
    for scene in scenes_data:
        chars = ", ".join(scene["characters"]) if scene["characters"] else ""
        block = f"CHARACTERS: {chars}\n{scene['description']}"
        blocks.append(block)
    return "\n---\n".join(blocks)


def _format_audio_scenes(scenes_data):
    """Format audio_scenes.txt — compatible with existing audio pipeline."""
    lines = []
    for scene in scenes_data:
        lines.append(f"SCENE: {scene['scene_id']}")
        lines.append("VOICE: Zephyr")
        lines.append(scene["narration"] or f"[Scene {scene['scene_number']} narration]")
        lines.append("")
    return "\n".join(lines)


def _format_video_scenes(scenes_data):
    """Format video_scenes.txt — compatible with existing video pipeline."""
    lines = []
    for scene in scenes_data:
        lines.append(f"SCENE: {scene['scene_id']}")
        lines.append(scene["description"])
        lines.append("")
    return "\n".join(lines)


def _build_image_prompts(scenes_data, fmt):
    """Build image_prompts.json."""
    return [
        {
            "scene_id": scene["scene_id"],
            "characters": scene["characters"],
            "environment": scene["environment"],
            "emotion": scene["emotion"],
            "description": scene["description"],
            "format": fmt,
        }
        for scene in scenes_data
    ]


# ============================================================
# METADATA & THUMBNAIL
# ============================================================

def run_metadata(run_id, paths, topic, fmt, approved_script_text,
                 scripture_references, dry_run=False):
    """
    Generate YouTube metadata and thumbnail plan from the APPROVED script.
    Saves to metadata/ directory.
    """
    _log(run_id, "🏷️  Generating metadata and thumbnail plan...")

    if dry_run:
        youtube_meta = _dry_run_metadata(topic)
        thumbnail_plan = _dry_run_thumbnail(topic)
    else:
        youtube_meta = _generate_youtube_metadata(
            topic, fmt, approved_script_text, scripture_references
        )
        thumbnail_plan = _generate_thumbnail_plan(
            topic, fmt, approved_script_text
        )

    sm.save_json(paths, "metadata", "youtube.json", youtube_meta)
    sm.save_json(paths, "metadata", "thumbnail.json", thumbnail_plan)
    _log(run_id, "   ✅ Metadata saved")
    return youtube_meta, thumbnail_plan


def _generate_youtube_metadata(topic, fmt, script_text, scripture_references):
    try:
        prompt = f"""Generate YouTube metadata for a Bible story video.

Topic: {topic}
Format: {fmt}
Script excerpt: {script_text[:1500]}

Respond ONLY with JSON (no markdown fences):
{{
  "title": "Engaging YouTube title (max 70 chars)",
  "description": "YouTube description (2-3 paragraphs, include scripture references)",
  "hashtags": ["hashtag1", "hashtag2", "hashtag3", "hashtag4", "hashtag5"],
  "tags": ["tag1", "tag2", "tag3"]
}}"""
        raw = sv._call_llm(prompt)
        return sv._parse_json_response(raw)
    except Exception as e:
        return _dry_run_metadata(topic, error=str(e))


def _generate_thumbnail_plan(topic, fmt, script_text):
    try:
        prompt = f"""Create a thumbnail plan for a Bible story video.

Topic: {topic}
Script excerpt: {script_text[:1000]}

Respond ONLY with JSON (no markdown fences):
{{
  "concept": "One sentence describing the thumbnail concept",
  "characters": ["character1", "character2"],
  "emotion": "dominant emotion",
  "composition": "description of layout and framing",
  "environment": "background setting",
  "optional_text": "short text overlay if any (or empty string)"
}}"""
        raw = sv._call_llm(prompt)
        return sv._parse_json_response(raw)
    except Exception as e:
        return _dry_run_thumbnail(topic, error=str(e))


def _dry_run_metadata(topic, error=None):
    return {
        "title": f"[DRY RUN] {topic}",
        "description": f"[DRY RUN] Bible story about {topic}.",
        "hashtags": ["#BibleStory", "#Animation", "#Faith"],
        "tags": ["bible", "story", "animation"],
        "dry_run": True,
        "error": error,
    }


def _dry_run_thumbnail(topic, error=None):
    return {
        "concept": f"[DRY RUN] Dramatic moment from {topic}",
        "characters": [],
        "emotion": "awe",
        "composition": "centered character, dramatic lighting",
        "environment": "biblical-era setting",
        "optional_text": "",
        "dry_run": True,
        "error": error,
    }


# ============================================================
# ROOT FILE SYNC — only called AFTER script PASS
# ============================================================

def _sync_breakdown_to_root(paths):
    """
    Copy approved breakdown files to the project root so the existing
    batch_image_generator / batch_audio_generator / batch_video_generator
    can find them without modification.

    Called ONLY after the script has passed the quality gate.
    This overwrites any staging files that script_engine wrote earlier.
    """
    import shutil

    file_map = {
        os.path.join(paths["breakdown"], "scenes.txt"):
            os.path.join(BASE_DIR, "scenes.txt"),
        os.path.join(paths["breakdown"], "audio_scenes.txt"):
            os.path.join(BASE_DIR, "audio_scenes.txt"),
        os.path.join(paths["breakdown"], "video_scenes.txt"):
            os.path.join(BASE_DIR, "video_scenes.txt"),
    }

    for src, dst in file_map.items():
        if os.path.exists(src):
            shutil.copy2(src, dst)
        else:
            print(f"   ⚠️  Sync: source not found — {src}")


# ============================================================
# MEDIA PIPELINE (calls existing scripts via subprocess)
# ============================================================

def _run_media_pipeline(run_id, paths):
    """
    Call the existing media pipeline stages via subprocess.
    Same pattern as topic_queue.py.
    Only called when run_media=True is passed to run_manual_pipeline().
    """
    import process_utils

    stages = [
        ("ref_characters", [PYTHON, "ref_character_generator.py"]),
        ("images",         [PYTHON, "batch_image_generator.py"]),
        ("audio",          [PYTHON, "batch_audio_generator.py"]),
        ("video",          [PYTHON, "batch_video_generator.py"]),
        ("assemble",       [PYTHON, "assemble_video.py"]),
    ]

    log_dir = paths["logs"]
    os.makedirs(log_dir, exist_ok=True)

    for stage_name, command in stages:
        _log(run_id, f"▶️  Running media stage: {stage_name}")
        sm.update_production(run_id, stage=stage_name)
        log_path = os.path.join(log_dir, f"{stage_name}.log")

        with open(log_path, "w", encoding="utf-8") as log_file:
            process = process_utils.popen_for_stage(command, log_file)
            returncode = process.wait()

        if returncode != 0:
            _log(run_id,
                 f"   ⚠️  {stage_name} failed (exit {returncode}) "
                 f"— see {log_path}")
            if stage_name == "images":
                raise RuntimeError(
                    "Image generation failed — pipeline stopped."
                )
        else:
            _log(run_id, f"   ✅ {stage_name} complete")


# ============================================================
# MAIN PIPELINE ORCHESTRATOR
# ============================================================

def run_manual_pipeline(
    topic,
    fmt,
    duration_secs,
    character_names=None,
    raw_script_text=None,
    supabase_topic_id=None,
    dry_run=False,
    run_media=False,
):
    """
    Full manual pipeline entry point.

    Args:
        topic:              Topic string or script label.
        fmt:                Format name (e.g. 'bible_story').
        duration_secs:      Target duration in seconds.
        character_names:    Optional list of character names.
        raw_script_text:    If provided, use as v001 (existing-script mode).
        supabase_topic_id:  Optional Supabase topic ID to link.
        dry_run:            If True, skip expensive LLM/media calls.
        run_media:          If True, call existing media pipeline after breakdown.

    Returns dict with:
        run_id, paths, status, approved_version, scene_count,
        characters, youtube_meta, thumbnail_plan
    """
    character_names = character_names or []
    input_type = "existing_script" if raw_script_text else "topic"

    # ---- CREATE PRODUCTION ----
    run_id, paths = sm.create_production(
        topic=topic,
        input_type=input_type,
        fmt=fmt,
        duration_secs=duration_secs,
        supabase_topic_id=supabase_topic_id,
    )

    print(f"\n{'='*60}")
    print(f"  RUN CREATED: {run_id}")
    print(f"  Topic: {topic}")
    print(f"  Format: {fmt}")
    print(f"  Duration: {duration_secs}s ({_scene_count(duration_secs)} scenes)")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")

    sm.update_production(run_id, stage="research")

    try:
        # ---- RESEARCH ----
        print("↓ BIBLE RESEARCH")
        story_facts, scripture_references = run_research(
            run_id, paths, topic, fmt
        )

        # ---- SCRIPT GENERATION / INGESTION ----
        print("\n↓ SCRIPT GENERATION")
        sm.update_production(run_id, stage="script_generation")
        script_text, version_label = generate_script(
            run_id, paths, topic, fmt, duration_secs,
            character_names, raw_script_text=raw_script_text,
            dry_run=dry_run,
        )
        print(f"  Script {version_label} ready")

        # ---- VERIFICATION LOOP ----
        print("\n↓ VERIFICATION")
        approved_script, approved_version, final_verification = run_verification_loop(
            run_id, paths, topic, fmt, duration_secs,
            character_names, script_text, version_label,
            scripture_references, dry_run=dry_run,
        )

        print(f"\n↓ QUALITY GATE PASSED ({approved_version})")

        # ---- BREAKDOWN ----
        print("\n↓ BREAKDOWN")
        sm.update_production(run_id, stage="breakdown")
        scenes_data, characters_data = run_breakdown(
            run_id, paths, approved_script, topic, fmt,
            duration_secs, character_names, story_facts,
            scripture_references, dry_run=dry_run,
        )

        # ---- METADATA & THUMBNAIL ----
        print("\n↓ METADATA")
        sm.update_production(run_id, stage="metadata")
        youtube_meta, thumbnail_plan = run_metadata(
            run_id, paths, topic, fmt, approved_script,
            scripture_references, dry_run=dry_run,
        )

        # ---- SYNC APPROVED FILES TO ROOT ----
        # Only happens here — after PASS — never during generation.
        _sync_breakdown_to_root(paths)

        sm.update_production(run_id,
                             stage="ready_for_media",
                             script_status="approved",
                             verification_status="passed")

        print(f"\n{'='*60}")
        print(f"  ✅ PIPELINE COMPLETE: {run_id}")
        print(f"  Approved script: {approved_version}")
        print(f"  Scenes: {len(scenes_data)}")
        print(f"  Root: {paths['root']}")
        print(f"{'='*60}\n")

        result = {
            "run_id": run_id,
            "paths": paths,
            "status": "ready_for_media",
            "approved_version": approved_version,
            "scene_count": len(scenes_data),
            "characters": characters_data,
            "youtube_meta": youtube_meta,
            "thumbnail_plan": thumbnail_plan,
        }

        # ---- OPTIONAL MEDIA PIPELINE ----
        if run_media and not dry_run:
            _run_media_pipeline(run_id, paths)

        return result

    except RuntimeError as e:
        error_msg = str(e)
        _log(run_id, f"❌ Pipeline stopped: {error_msg}")
        sm.update_production(run_id, stage="failed", error=error_msg[:500])
        raise
