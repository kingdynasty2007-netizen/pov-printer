# ============================================================
# FILE: production_manager.py
# PURPOSE: Orchestrates the full manual production pipeline.
#
# FLOW:
#   INPUT → RESEARCH → STORY (v001, full prose) → STORY VERIFY
#   → STORY REWRITE LOOP → STORY PASS GATE
#   → BREAKDOWN GENERATION (scenes, ALWAYS adapted from the approved
#     story — never invented independently from the topic)
#   → CONSISTENCY VERIFY (scenes vs. the approved story)
#   → CONSISTENCY REWRITE LOOP → HARD PASS GATE
#   → BREAKDOWN (parse into structured artifacts) → METADATA → THUMBNAIL
#   → (existing media systems)
#
# CHANGE: the full story is now a first-class stage with its own
# versioned folder (story/versions/) and its own quality gate — the
# same hook/pacing/biblical_accuracy/etc. checks that used to run on
# the scene-broken text now run on the actual narrative. The scene
# breakdown is generated FROM that approved story (script_engine.run()
# always receives it as raw_script_text) and is then checked for
# CONSISTENCY against it, not re-graded for quality from scratch.
#
# Does NOT run image/audio/video generation itself.
# Calls the existing script_engine, bible_strory_lookup,
# ref_character_generator, thumbnail_generator, batch_* generators
# via subprocess (same pattern as topic_queue.py).
#
# The media pipeline now runs "thumbnail" immediately after
# "ref_characters" (the thumbnail uses the character references as
# face anchors), and transparently falls back to retry_refs.py when
# the reference stage comes back incomplete.
#
# YouTube UPLOAD: NOT implemented here. Only the thumbnail image is
# produced so far; uploading it still needs OAuth + a publish stage.
# Automatic queue: NOT implemented here.
#
# KNOWN BREAKING CHANGE: tests/test_pipeline.py calls run_verification_loop()
# with the OLD signature (scripture_references=...) and patches
# sv.verify_dry_run — that function now does scene-vs-story consistency
# checking and calls sv.verify_scenes_against_story_dry_run instead.
# Those tests need updating to match (not done in this pass).
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
import run_paths

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
# FULL STORY GENERATION (new — upstream of the scene breakdown)
# ============================================================

def _dry_run_story(topic):
    return f"[DRY RUN STORY] This is a placeholder full story for topic: {topic}."

def generate_story(run_id, paths, topic, fmt, duration_secs,
                   raw_story_text=None, scripture_references=None, dry_run=False):
    """
    Generate (or ingest) the FULL STORY — plain prose, no scene markers,
    no camera direction. This is the source of truth everything
    downstream (scene breakdown, metadata) is adapted from.

    scripture_references: passed straight through to
    script_engine.generate_story() so the story is grounded in the
    same passages the quality gate later verifies it against
    (bible_story format only — empty/None for other formats).

    Saves as the next story version (v001 on first run).
    Returns (story_text, version_label).
    """
    if raw_story_text:
        _log(run_id, "📄 Ingesting provided text as story v001...")
        story_text = raw_story_text
    elif dry_run:
        _log(run_id, "🧪 [DRY RUN] Generating placeholder story...")
        story_text = _dry_run_story(topic)
    else:
        _log(run_id, "📝 Writing full story...")
        import script_engine
        story_text = script_engine.generate_story(
            topic, fmt, duration_secs, scripture_references=scripture_references,
        )

    version_label = sm.save_story_version(paths, story_text)
    sm.update_production(run_id, stage="story_generated")
    return story_text, version_label


def rewrite_story(run_id, paths, topic, fmt, duration_secs,
                  previous_story_text, verification_result,
                  scripture_references=None, dry_run=False):
    """
    Rewrite the full story based on quality-gate feedback.
    scripture_references is threaded through the same way generate_story()
    does it, so a rewrite stays grounded too, not just the first draft.
    Returns (new_story_text, new_version_label).
    """
    required_fixes = verification_result.get("required_fixes", [])
    fixes_text = (
        "\n".join(f"- {f}" for f in required_fixes)
        if required_fixes else "- General quality improvement needed."
    )

    if dry_run:
        _log(run_id, "🧪 [DRY RUN] Generating story rewrite placeholder...")
        new_story_text = _dry_run_story(f"{topic} [REWRITE]")
    else:
        _log(run_id, "🔄 Rewriting story based on verification feedback...")
        import script_engine

        rewrite_topic = (
            f"REWRITE INSTRUCTIONS — the previous version of this story "
            f"failed quality checks.\nRequired fixes:\n{fixes_text}\n\n"
            f"Previous story (for reference — do NOT copy it, fix the "
            f"issues):\n{previous_story_text[:3000]}\n\n"
            f"Now write an improved version of the story for topic: {topic}\n"
        )
        new_story_text = script_engine.generate_story(
            rewrite_topic, fmt, duration_secs, scripture_references=scripture_references,
        )

    new_version_label = sm.save_story_version(paths, new_story_text)
    sm.update_production(run_id, stage="story_rewritten")
    _log(run_id, f"   ✅ Story rewrite saved as {new_version_label}")
    return new_story_text, new_version_label
  
def run_story_verification_loop(run_id, paths, topic, fmt, duration_secs,
                                initial_story_text, initial_version_label,
                                scripture_references, dry_run=False):
    """
    Verify → rewrite → re-verify loop for the FULL STORY. This is the
    real content quality gate (hook, pacing, biblical_accuracy, climax,
    etc.) — it runs ONCE here, before any scene breakdown exists.

    Returns (approved_story_text, approved_story_version_label, final_verification_result)
    or raises RuntimeError if FAILED_QUALITY_GATE.
    """
    scene_count = _scene_count(duration_secs)
    current_story = initial_story_text
    current_version = initial_version_label
    rewrite_count = 0

    while True:
        _log(run_id, f"🔍 Verifying story {current_version}...")
        sm.update_production(run_id, stage="verifying_story", verification_status="running")

        if dry_run:
            force_pass = (rewrite_count >= 1)
            result = sv.verify_dry_run(
                current_story, topic, fmt, duration_secs, scene_count,
                force_pass=force_pass,
            )
        else:
            result = sv.verify(
                current_story, topic, fmt, duration_secs, scene_count,
                scripture_references=scripture_references,
            )

        sm.save_json(paths, "verification", f"story_{current_version}.json", result)
        _log(run_id, f"   Status: {result['status']}")

        if result["passed"]:
            _log(run_id, f"   ✅ STORY QUALITY GATE PASSED ({current_version})")
            sm.update_production(run_id, verification_status="story_passed", stage="story_approved")
            sm.set_current_story(paths, current_story)
            return current_story, current_version, result

        if result["status"] == "FAILED_QUALITY_GATE":
            sm.update_production(run_id, verification_status="story_failed_quality_gate",
                                 stage="failed", error="Story failed quality gate — pipeline stopped.")
            raise RuntimeError(
                f"❌ Story FAILED_QUALITY_GATE on {current_version}. "
                f"Required fixes: {result.get('required_fixes', [])}"
            )

        is_system_error = result.get("_parse_error", False) or result.get("_server_error", False)

        if is_system_error:
            _log(run_id, "   ⚠️  SYSTEM ERROR during story verification — retrying same version (rewrite slot NOT consumed)")
        else:
            rewrite_count += 1
            _log(run_id, f"   ⚠️  Story NEEDS_REWRITE (attempt {rewrite_count}/{MAX_REWRITE_ATTEMPTS})")
            for fix in result.get("required_fixes", []):
                _log(run_id, f"      → {fix}")

        if rewrite_count >= MAX_REWRITE_ATTEMPTS and not is_system_error:
            sm.update_production(run_id, verification_status="story_failed_quality_gate",
                                 stage="failed", error=f"Exceeded {MAX_REWRITE_ATTEMPTS} story rewrite attempts.")
            raise RuntimeError(
                f"❌ Story still failing after {MAX_REWRITE_ATTEMPTS} rewrite attempt(s). "
                f"Pipeline stopped. Last status: {result['status']}"
            )

        sm.update_production(run_id, stage="rewriting_story", verification_status="story_needs_rewrite")

        if not is_system_error:
            current_story, current_version = rewrite_story(
                run_id, paths, topic, fmt, duration_secs,
                current_story, result, scripture_references=scripture_references,
                dry_run=dry_run,
            )


# ============================================================
# SCENE BREAKDOWN GENERATION (always adapted from the approved story)
# ============================================================

def _dry_run_script(topic, scene_count):
    """Minimal placeholder script for dry-run / test mode."""
    lines = [f"[DRY RUN SCRIPT] Topic: {topic}\n"]
    for i in range(1, scene_count + 1):
        lines.append(f"SCENE {i:03d}: Placeholder scene {i} for {topic}.")
        if i < scene_count:
            lines.append("---")
    return "\n".join(lines)


def generate_script(run_id, paths, topic, fmt, duration_secs,
                    character_names, story_text, dry_run=False):
    """
    Generate the scene-by-scene breakdown — ALWAYS adapted from the
    approved story_text (never invented independently from the topic).
    This is what keeps the breakdown from drifting off-story.
    Saves as the next version (v001 on first run).
    Returns (script_text, version_label).
    """
    scene_count = _scene_count(duration_secs)

    if dry_run:
        _log(run_id, "🧪 [DRY RUN] Generating placeholder script...")
        script_text = _dry_run_script(topic, scene_count)
    else:
        _log(run_id, f"✍️  Breaking approved story into {scene_count} scenes...")
        import script_engine
        result = script_engine.run(
            topic=topic,
            style=fmt,
            duration_secs=duration_secs,
            scene_count=scene_count,
            character_names=character_names,
            topic_id=None,
            raw_script_text=story_text,
        )
        scenes_txt = result.get("scenes_txt", "")
        audio_txt = result.get("audio_txt", "")
        script_text = f"=== SCENES ===\n{scenes_txt}\n\n=== AUDIO ===\n{audio_txt}"

    version_label = sm.save_script_version(paths, script_text)
    sm.update_production(run_id, current_script_version=version_label, script_status="generated")
    return script_text, version_label


def rewrite_script(run_id, paths, topic, fmt, duration_secs,
                   character_names, previous_script_text,
                   verification_result, story_text, dry_run=False):
    """
    Regenerate the scene breakdown from the SAME approved story,
    incorporating consistency-check feedback (e.g. "scene 4 shows an
    event not in the story").
    Returns (new_script_text, new_version_label).
    """
    scene_count = _scene_count(duration_secs)
    required_fixes = verification_result.get("required_fixes", [])
    fixes_text = (
        "\n".join(f"- {f}" for f in required_fixes)
        if required_fixes else "- General consistency improvement needed."
    )

    if dry_run:
        _log(run_id, "🧪 [DRY RUN] Generating rewrite placeholder...")
        new_script_text = _dry_run_script(f"{topic} [REWRITE]", scene_count)
    else:
        _log(run_id, "🔄 Rebreaking story into scenes based on consistency feedback...")
        import script_engine

        rewrite_instruction = (
            f"IMPORTANT — the previous scene breakdown of this story drifted "
            f"from it. Fix this when breaking it down again:\n{fixes_text}\n\n"
            f"Break down ONLY the story below — do not invent new plot, "
            f"characters, or events beyond what it contains:\n\n{story_text}"
        )

        result = script_engine.run(
            topic=topic,
            style=fmt,
            duration_secs=duration_secs,
            scene_count=scene_count,
            character_names=character_names,
            topic_id=None,
            raw_script_text=rewrite_instruction,
        )
        scenes_txt = result.get("scenes_txt", "")
        audio_txt = result.get("audio_txt", "")
        new_script_text = f"=== SCENES ===\n{scenes_txt}\n\n=== AUDIO ===\n{audio_txt}"

    new_version_label = sm.save_script_version(paths, new_script_text)
    sm.update_production(run_id, current_script_version=new_version_label, script_status="rewritten")
    _log(run_id, f"   ✅ Rewrite saved as {new_version_label}")
    return new_script_text, new_version_label


# ============================================================
# CONSISTENCY VERIFICATION LOOP (scenes vs. approved story)
# ============================================================

def run_verification_loop(run_id, paths, topic, fmt, duration_secs,
                          character_names, initial_script_text,
                          initial_version_label, story_text,
                          dry_run=False):
    """
    Verify the SCENE BREAKDOWN against the approved story — not a
    standalone quality gate (that already happened at the story stage).
    Checks the breakdown stayed faithful: same characters, same events,
    same order, nothing invented or dropped.

    Verify → rewrite → re-verify loop. Enforces MAX_REWRITE_ATTEMPTS.
    Server/parse errors do NOT burn a rewrite slot.
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
        _log(run_id, f"🔍 Verifying {current_version} against approved story...")
        sm.update_production(run_id, stage="verifying", verification_status="running")

        if dry_run:
            force_pass = (rewrite_count >= 1)
            result = sv.verify_scenes_against_story_dry_run(
                current_script, story_text, force_pass=force_pass,
            )
        else:
            result = sv.verify_scenes_against_story(
                current_script, story_text, topic, fmt,
            )

        sm.save_verification(paths, current_version, result)
        _log(run_id, f"   Status: {result['status']}")

        # ---- Scene count sanity check ----
        expected_scenes = scene_count
        actual_scenes = _count_scenes_in_script(current_script)
        if (
            result["status"] == "FAILED_QUALITY_GATE"
            and actual_scenes < expected_scenes * 0.8
        ):
            _log(run_id,
                 f"   ⚠️  Script has {actual_scenes}/{expected_scenes} scenes — "
                 f"overriding FAILED_QUALITY_GATE to NEEDS_REWRITE "
                 f"(generation was truncated, not a consistency failure)")
            result["status"] = "NEEDS_REWRITE"
            result["passed"] = False
            if not result.get("required_fixes"):
                result["required_fixes"] = [
                    f"Script was truncated — only {actual_scenes} of "
                    f"{expected_scenes} scenes were generated. "
                    f"Regenerate with the full scene count."
                ]

        if result["passed"]:
            _log(run_id, f"   ✅ BREAKDOWN MATCHES STORY ({current_version})")
            sm.update_production(run_id,
                                 verification_status="passed",
                                 script_status="approved",
                                 stage="approved")
            sm.set_current_script(paths, current_script)
            return current_script, current_version, result

        if result["status"] == "FAILED_QUALITY_GATE":
            sm.update_production(run_id,
                                 verification_status="failed_quality_gate",
                                 stage="failed",
                                 error="Breakdown diverged from approved story — pipeline stopped.")
            raise RuntimeError(
                f"❌ FAILED_QUALITY_GATE on {current_version}. "
                f"Required fixes: {result.get('required_fixes', [])}"
            )

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

        if not is_system_error:
            current_script, current_version = rewrite_script(
                run_id, paths, topic, fmt, duration_secs,
                character_names, current_script, result, story_text,
                dry_run=dry_run,
            )


# ============================================================
# POST-PASS BREAKDOWN (unchanged — parses the approved scene text)
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

    characters_data = _extract_characters(
        approved_script_text, character_names, fmt
    )
    sm.save_json(paths, "breakdown", "characters.json", characters_data)

    scenes_data = _extract_scenes(approved_script_text, scene_count, fmt)
    scenes_txt = _format_scenes_txt(scenes_data)
    sm.save_text(paths, "breakdown", "scenes.txt", scenes_txt)

    audio_txt = _format_audio_scenes(scenes_data)
    sm.save_text(paths, "breakdown", "audio_scenes.txt", audio_txt)

    video_txt = _format_video_scenes(scenes_data)
    sm.save_text(paths, "breakdown", "video_scenes.txt", video_txt)

    image_prompts = _build_image_prompts(scenes_data, fmt)
    sm.save_json(paths, "breakdown", "image_prompts.json", image_prompts)

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

    raw_blocks = (
        [b.strip() for b in scenes_section.split("---") if b.strip()]
        if "---" in scenes_section
        else ([scenes_section.strip()] if scenes_section.strip() else [])
    )

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
        for line in lines[1:]:
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
# METADATA & THUMBNAIL (unchanged — now sourced from the full story)
# ============================================================

def run_metadata(run_id, paths, topic, fmt, source_text,
                 scripture_references, dry_run=False):
    """
    Generate YouTube metadata and thumbnail plan from the approved
    STORY (richer prose reads better than the scene-broken format for
    a YouTube description). Saves to metadata/ directory.
    """
    _log(run_id, "🏷️  Generating metadata and thumbnail plan...")

    if dry_run:
        youtube_meta = _dry_run_metadata(topic)
        thumbnail_plan = _dry_run_thumbnail(topic)
    else:
        youtube_meta = _generate_youtube_metadata(
            topic, fmt, source_text, scripture_references
        )
        thumbnail_plan = _generate_thumbnail_plan(
            topic, fmt, source_text
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
# MEDIA PIPELINE (calls existing scripts via subprocess)
# ============================================================

def _run_one_media_stage(run_id, log_dir, stage_env, stage_name, command):
    """Run one media stage as a subprocess, streaming a heartbeat dot so the
    terminal never looks frozen. Returns the process return code."""
    import process_utils

    _log(run_id, f"▶️  Running media stage: {stage_name}")
    sm.update_production(run_id, stage=stage_name)
    log_path = os.path.join(log_dir, f"{stage_name}.log")

    with open(log_path, "w", encoding="utf-8") as log_file:
        process = process_utils.popen_for_stage(
            command, log_file, cwd=BASE_DIR, env=stage_env,
        )
        try:
            while process.poll() is None:
                time.sleep(5)
                print(".", end="", flush=True)   # heartbeat
        except KeyboardInterrupt:
            process_utils.kill_process_tree(process)
            raise
        print()
        return process.returncode


def _reference_characters_usable(run_id):
    """True only if reference_characters.json exists AND has at least one
    entry. image_core.py hard-stops at import without it, so a "successful"
    ref_characters stage that produced nothing usable is still a failure."""
    path = run_paths.get_paths(run_id, announce=False)["ref_characters_file"]
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            return bool(json.load(f))
    except (ValueError, OSError):
        return False


def _run_media_pipeline(run_id, paths):
    """
    Call the existing media pipeline stages via subprocess.
    Every stage gets POV_RUN_ID, so references, images, audio, video, the
    manifest and the final video all land in data/productions/<run_id>/
    and never in the shared root folders.
    Only called when run_media=True is passed to run_manual_pipeline().

    Two deliberate behaviours:
      - A ref_characters failure triggers retry_refs.py, which regenerates
        ONLY what is still missing. This exists because
        ref_character_generator.py exits 0 on a partial success (some
        locations succeeded but every character failed), which would
        otherwise walk into batch_image_generator.py and kill the run at
        import time.
      - A thumbnail failure is NON-fatal. The video is already made and
        lives on disk; a missing thumbnail must not discard that.
    """
    stages = [
        ("ref_characters", [PYTHON, "ref_character_generator.py"]),
        # Runs right after refs, not at the end: it uses the character
        # references as face anchors, so it needs them to already exist.
        ("thumbnail",      [PYTHON, "thumbnail_generator.py"]),
        ("images",         [PYTHON, "batch_image_generator.py"]),
        ("audio",          [PYTHON, "batch_audio_generator.py"]),
        ("video",          [PYTHON, "batch_video_generator.py"]),
        ("assemble",       [PYTHON, "assemble_video.py"]),
    ]

    log_dir = paths["logs"]
    os.makedirs(log_dir, exist_ok=True)

    stage_env = dict(os.environ)
    stage_env[run_paths.RUN_ENV_VAR] = run_id
    stage_env.pop(run_paths.LEGACY_ENV_VAR, None)

    for stage_name, command in stages:
        returncode = _run_one_media_stage(
            run_id, log_dir, stage_env, stage_name, command
        )
        log_path = os.path.join(log_dir, f"{stage_name}.log")

        if stage_name == "ref_characters":
            # Gate on the FILE, not just the exit code: a partial ref run
            # exits 0 while leaving reference_characters.json unwritten.
            if returncode != 0 or not _reference_characters_usable(run_id):
                _log(run_id,
                     f"   ⚠️  ref_characters incomplete (exit {returncode}) "
                     f"— running retry_refs.py for the missing ones")
                retry_rc = _run_one_media_stage(
                    run_id, log_dir, stage_env, "retry_refs",
                    [PYTHON, "retry_refs.py"],
                )
                if retry_rc != 0 or not _reference_characters_usable(run_id):
                    _log(run_id,
                         f"   ❌ Reference generation failed after retry "
                         f"(exit {retry_rc}) — see "
                         f"{os.path.join(log_dir, 'retry_refs.log')}")
                    raise RuntimeError(
                        "Reference generation failed — the image stage cannot "
                        "run without reference_characters.json. Pipeline stopped."
                    )
                _log(run_id, "   ✅ References complete after retry")
            else:
                _log(run_id, "   ✅ ref_characters complete")
            continue

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

    final_video = run_paths.get_paths(run_id)["final_output"]
    if os.path.exists(final_video):
        size_mb = os.path.getsize(final_video) / (1024 * 1024)
        _log(run_id, f"🎬 Final video: {final_video} ({size_mb:.1f} MB)")
        sm.update_production(run_id, stage="complete")
    else:
        _log(run_id, "⚠️  Media pipeline finished but no final video was produced")


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
        topic:              Topic string or story label.
        fmt:                Format name (e.g. 'bible_story').
        duration_secs:      Target duration in seconds.
        character_names:    Optional list of character names.
        raw_script_text:    If provided, used as the STORY v001 directly
                             (existing-script mode) — no LLM story
                             generation, but it still goes through the
                             story quality gate like any other story.
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

    # Route EVERY file this run produces into data/productions/<run_id>/.
    # script_engine (in-process) and all media subprocess stages read this.
    # POV_LEGACY is cleared for the duration: if the caller's shell happens to
    # have it set (e.g. they exported it to run the old queue by hand), an
    # explicit POV_RUN_ID still wins, but dropping it stops a stale opt-out from
    # leaking into the subprocess stage environments.
    _previous_run_id_env = os.environ.get(run_paths.RUN_ENV_VAR)
    _previous_legacy_env = os.environ.pop(run_paths.LEGACY_ENV_VAR, None)
    os.environ[run_paths.RUN_ENV_VAR] = run_id

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
        print("↓ RESEARCH")
        story_facts, scripture_references = run_research(
            run_id, paths, topic, fmt
        )

        # ---- FULL STORY GENERATION / INGESTION ----
        print("\n↓ STORY GENERATION")
        sm.update_production(run_id, stage="story_generation")
        story_text, story_version = generate_story(
            run_id, paths, topic, fmt, duration_secs,
            raw_story_text=raw_script_text, scripture_references=scripture_references,
            dry_run=dry_run,
        )
        print(f"  Story {story_version} ready")

        # ---- STORY VERIFICATION LOOP ----
        print("\n↓ STORY VERIFICATION")
        approved_story, approved_story_version, story_verification = run_story_verification_loop(
            run_id, paths, topic, fmt, duration_secs,
            story_text, story_version, scripture_references, dry_run=dry_run,
        )
        print(f"\n↓ STORY QUALITY GATE PASSED ({approved_story_version})")

        # ---- SCENE BREAKDOWN GENERATION (adapted from the approved story) ----
        print("\n↓ SCENE BREAKDOWN GENERATION")
        sm.update_production(run_id, stage="script_generation")
        script_text, version_label = generate_script(
            run_id, paths, topic, fmt, duration_secs,
            character_names, approved_story, dry_run=dry_run,
        )
        print(f"  Script {version_label} ready")

        # ---- CONSISTENCY VERIFICATION LOOP ----
        print("\n↓ CONSISTENCY VERIFICATION")
        approved_script, approved_version, final_verification = run_verification_loop(
            run_id, paths, topic, fmt, duration_secs,
            character_names, script_text, version_label,
            approved_story, dry_run=dry_run,
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
            run_id, paths, topic, fmt, approved_story,
            scripture_references, dry_run=dry_run,
        )

        sm.update_production(run_id,
                             stage="ready_for_media",
                             script_status="approved",
                             verification_status="passed")

        print(f"\n{'='*60}")
        print(f"  ✅ PIPELINE COMPLETE: {run_id}")
        print(f"  Approved story: {approved_story_version}")
        print(f"  Approved script: {approved_version}")
        print(f"  Scenes: {len(scenes_data)}")
        print(f"  Root: {paths['root']}")
        print(f"{'='*60}\n")

        result = {
            "run_id": run_id,
            "paths": paths,
            "status": "ready_for_media",
            "approved_story_version": approved_story_version,
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
    finally:
        if _previous_run_id_env is None:
            os.environ.pop(run_paths.RUN_ENV_VAR, None)
        else:
            os.environ[run_paths.RUN_ENV_VAR] = _previous_run_id_env
        if _previous_legacy_env is not None:
            os.environ[run_paths.LEGACY_ENV_VAR] = _previous_legacy_env