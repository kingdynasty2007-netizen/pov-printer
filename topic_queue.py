# ============================================================
# FILE: topic_queue.py
# PURPOSE: Automatic daily runner. Zero input at run time —
# everything needed was already set when the topic was added.
#
# ADD:  python topic_queue.py --add --topic "#pov A chef's day" --duration 5:30 [--characters "Maria,Antoine"]
# LIST: python topic_queue.py --list
# RUN:  python topic_queue.py            (no flags — pulls next pending topic)
# ============================================================

import os
import sys
import json
import argparse
import subprocess
from datetime import datetime, timezone
from dotenv import load_dotenv

# The queue still uses the flat folders at the project root, so pin that here,
# BEFORE any project module resolves a path. Every stage it spawns inherits
# this env var, which is what keeps the legacy queue working now that stage
# scripts default to the newest run instead. run_paths.get_paths() stays
# env-driven, so this also fixes the ref_characters_file check below.
os.environ["POV_LEGACY"] = "1"

from db import supabase
import script_engine
import run_lock
import process_utils
import run_paths
from topic_utils import extract_style_tag
from duration_utils import parse_duration, format_duration

load_dotenv()

PYTHON = sys.executable
LOG_FOLDER = "pipeline_logs"
STAGE_TIMEOUT_SECONDS = 4 * 3600
STOP_CHECK_INTERVAL = 2


def add_topic(raw_topic, duration_text, character_names_text):
    style, topic_text = extract_style_tag(raw_topic)
    if not style:
        print("❌ No #style tag found. Format: #style_name Your topic here")
        sys.exit(1)

    try:
        duration_secs = parse_duration(duration_text)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    character_names = character_names_text or ""

    try:
        response = supabase.table("topics").insert({
            "topic": topic_text,
            "style": style,
            "status": "pending",
            "duration_secs": duration_secs,
            "character_names": character_names,
        }).execute()
        topic_id = response.data[0]["id"]
        print(f"✅ Topic added (id={topic_id}): [{style}] {topic_text} — {format_duration(duration_secs)}")
    except Exception as e:
        print(f"❌ Could not add topic: {e}")


def list_topics():
    try:
        response = supabase.table("topics").select("*").order("id").execute()
        topics = response.data
        if not topics:
            print("📭 No topics in queue")
            return
        print(f"\n{'ID':<6} {'STATUS':<12} {'STYLE':<12} {'DURATION':<9} TOPIC")
        print("-" * 80)
        for t in topics:
            dur = format_duration(t.get("duration_secs") or 0)
            print(f"{t['id']:<6} {t['status']:<12} {t['style']:<12} {dur:<9} {t['topic']}")
        print()
    except Exception as e:
        print(f"❌ Could not fetch topics: {e}")


def get_next_pending_topic():
    try:
        response = (
            supabase.table("topics").select("*")
            .eq("status", "pending").order("id").limit(1).execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"❌ Could not fetch next topic: {e}")
        return None


def update_topic_status(topic_id, status):
    try:
        supabase.table("topics").update({
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()
    except Exception as e:
        print(f"⚠️  Could not update topic status: {e}")


def create_run(topic_id):
    try:
        response = supabase.table("runs").insert({"topic_id": topic_id, "status": "started"}).execute()
        return response.data[0]["id"]
    except Exception as e:
        print(f"⚠️  Could not create run record: {e}")
        return None


def update_run(run_id, updates):
    if not run_id:
        return
    try:
        supabase.table("runs").update(updates).eq("id", run_id).execute()
    except Exception as e:
        print(f"⚠️  Could not update run: {e}")


def run_stage(name, command):
    """Popen + poll instead of a blocking subprocess.run — lets a stop
    request actually interrupt a stage mid-run, not just block the next one."""
    os.makedirs(LOG_FOLDER, exist_ok=True)
    log_path = os.path.join(LOG_FOLDER, f"{name}.log")
    print(f"\n▶️  Running: {name}")
    run_lock.mark_stage(name)

    with open(log_path, "w", encoding="utf-8") as log_file:
        process = process_utils.popen_for_stage(command, log_file)
        start = datetime.now()

        while True:
            if run_lock.stop_requested():
                print(f"\n⏹️  Stop requested — terminating {name}...")
                process_utils.kill_process_tree(process)
                return False, "stopped"
            try:
                returncode = process.wait(timeout=STOP_CHECK_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                if (datetime.now() - start).total_seconds() > STAGE_TIMEOUT_SECONDS:
                    print(f"\n⏱️  {name} timed out — terminating")
                    process_utils.kill_process_tree(process)
                    return False, "timeout"
                continue

    if returncode == 0:
        print(f"✅ {name} complete")
        return True, "ok"
    print(f"❌ {name} failed (exit {returncode}) — see {log_path}")
    return False, "failed"


def _reference_characters_usable():
    """True only if reference_characters.json exists AND has at least one
    entry. image_core.py does sys.exit(1) at import without it, so a
    'successful' ref stage that produced nothing usable is still a
    failure — and that is exactly what a partial ref run looks like."""
    path = run_paths.get_paths(announce=False)["ref_characters_file"]
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return bool(content) and bool(json.loads(content))
    except (ValueError, OSError):
        return False


def run_daily():
    print(f"\n=== TOPIC QUEUE RUNNER — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    topic = get_next_pending_topic()
    if not topic:
        print("✅ No pending topics. Add one with --add.")
        return

    topic_id = topic["id"]
    topic_text = topic["topic"]
    style = topic["style"]
    duration_secs = topic.get("duration_secs") or 60
    character_names = [c.strip() for c in (topic.get("character_names") or "").split(",") if c.strip()]
    scene_count = max(1, duration_secs // script_engine.SECONDS_PER_SCENE)

    print(f"📌 Topic (id={topic_id}): [{style}] {topic_text} — {format_duration(duration_secs)} ({scene_count} scenes)")

    update_topic_status(topic_id, "running")
    run_lock.mark_started(topic_id, topic_text)
    run_id = create_run(topic_id)

    def finish(status, run_updates=None):
        update_topic_status(topic_id, status)
        run_lock.mark_finished()
        if run_updates:
            update_run(run_id, {**run_updates, "finished_at": datetime.now(timezone.utc).isoformat()})

    print("\n--- SCRIPT GENERATION ---")
    try:
        script_engine.run(
            topic=topic_text, style=style, duration_secs=duration_secs,
            scene_count=scene_count, character_names=character_names, topic_id=topic_id,
        )
    except Exception as e:
        print(f"❌ Script generation failed: {e}")
        finish("failed", {"status": "failed"})
        return

    if run_lock.stop_requested():
        print("\n⏹️  Stopped after script generation.")
        finish("pending")   # not "failed" — resumes cleanly next run
        return

    print("\n--- REFERENCE CHARACTERS ---")
    ok, reason = run_stage("ref_characters", [PYTHON, "ref_character_generator.py"])
    if reason == "stopped":
        finish("pending")
        return
    # ref_character_generator.py exits 0 on a PARTIAL success (e.g. every
    # character failed but a location succeeded), leaving
    # reference_characters.json unwritten. batch_image_generator.py then
    # hard-stops at import via image_core. So gate on the file, not the
    # exit code, and give retry_refs.py a shot at only what's missing.
    if not ok or not _reference_characters_usable():
        print("⚠️  Reference generation incomplete — retrying only the missing ones")
        ok2, reason2 = run_stage("retry_refs", [PYTHON, "retry_refs.py"])
        if reason2 == "stopped":
            finish("pending")
            return
        if not ok2 or not _reference_characters_usable():
            print("❌ Reference generation failed after retry — stopping pipeline")
            print("   reference_characters.json is required by batch_image_generator.py")
            finish("failed", {"status": "failed"})
            return
    print("✅ References ready")

    for stage_name, script in [("images", "batch_image_generator.py"),
                                ("video", "batch_video_generator.py"),
                                ("audio", "batch_audio_generator.py")]:
        print(f"\n--- {stage_name.upper()} ---")
        ok, reason = run_stage(stage_name, [PYTHON, script])
        if reason == "stopped":
            finish("pending")
            return
        if stage_name == "images" and not ok:
            print("❌ Image generation failed — stopping pipeline")
            finish("failed", {"status": "failed"})
            return
        update_run(run_id, {"status": f"{stage_name}_done"})

    print("\n--- ASSEMBLY ---")
    ok, reason = run_stage("assemble", [PYTHON, "assemble_video.py"])
    if reason == "stopped":
        finish("pending")
        return

    if ok and os.path.exists("final_video.mp4"):
        size_mb = os.path.getsize("final_video.mp4") / (1024 * 1024)
        print(f"\n🎬 final_video.mp4 ready ({size_mb:.1f} MB)")
        finish("done", {"status": "assembled", "output_path": "final_video.mp4"})
    else:
        finish("failed", {"status": "failed"})

    print(f"\n✅ Topic (id={topic_id}) marked DONE")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--add", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--topic")
    parser.add_argument("--duration")
    parser.add_argument("--characters", default="")
    args = parser.parse_args()

    if args.add:
        if not args.topic or not args.duration:
            print('Usage: python topic_queue.py --add --topic "#style Topic text" --duration 5:30 [--characters "A,B"]')
            sys.exit(1)
        add_topic(args.topic, args.duration, args.characters)
    elif args.list:
        list_topics()
    else:
        try:
            run_daily()
        except KeyboardInterrupt:
            print("\n\n⏹️  Stopped.")


if __name__ == "__main__":
    main()