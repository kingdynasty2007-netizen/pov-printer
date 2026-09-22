# ============================================================
# FILE: manual_runner.py
# PURPOSE: Run a topic OR an uploaded script RIGHT NOW, interactively.
# Writes scenes.txt / video_scenes.txt / audio_scenes.txt /
# ref_prompts.json and STOPS — never runs image/video/audio/assembly
# itself. Checks whether topic_queue.py's automatic run is active
# first.
#
# CHANGE: fully interactive now (no command-line flags needed) —
# asks for topic-or-script, lists available formats by number, asks
# for duration in mm:ss, asks for optional character names.
# CHANGE: a failed script write now correctly marks the topic
# "failed" in Supabase instead of leaving it stuck as "running"
# forever.
#
# RUN: python manual_runner.py
# ============================================================

import os
import sys
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from db import supabase
import script_engine
import run_lock
from duration_utils import parse_duration, format_duration

load_dotenv()


def handle_conflict():
    status = run_lock.get_background_status()
    topic_text = status.get("topic_text", "unknown") if status else "unknown"
    stage = status.get("stage", "unknown") if status else "unknown"

    print(f"\n⚠️  A background run is currently active: \"{topic_text}\" (stage: {stage})")
    print("What do you want to do?")
    print("  1) Stop it now, then run mine")
    print("  2) Let it finish, then run mine automatically after")
    print("  3) Run mine anyway, alongside it (risk: file conflicts)")
    choice = input("Choice (1/2/3): ").strip()

    if choice == "1":
        run_lock.request_stop()
        print("⏹️  Stop requested — waiting for it to actually stop...")
        while run_lock.is_background_running():
            print(".", end="", flush=True)
            time.sleep(3)
        print("\n✅ Background run stopped.")
        return True

    elif choice == "2":
        print("⏳ Waiting for the background run to finish...")
        while run_lock.is_background_running():
            print(".", end="", flush=True)
            time.sleep(5)
        print("\n✅ Background run finished — proceeding.")
        return True

    elif choice == "3":
        print("⚠️  Proceeding alongside the background run — file conflicts are possible.")
        return True

    else:
        print("Cancelled.")
        return False


def create_manual_topic_row(style, topic_text, duration_secs, character_names):
    try:
        response = supabase.table("topics").insert({
            "topic": topic_text,
            "style": style,
            "status": "running",
            "duration_secs": duration_secs,
            "character_names": ",".join(character_names),
        }).execute()
        return response.data[0]["id"]
    except Exception as e:
        print(f"⚠️  Could not save topic to Supabase: {e}")
        return None


def mark_script_ready(topic_id):
    if not topic_id:
        return
    try:
        supabase.table("topics").update({
            "status": "script_ready",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()
    except Exception as e:
        print(f"⚠️  Could not update topic status: {e}")


def mark_topic_failed(topic_id, reason):
    """Fix for a real bug: previously a failed script write left the
    topic stuck as 'running' in Supabase forever."""
    if not topic_id:
        return
    try:
        supabase.table("topics").update({
            "status": "failed",
            "error": str(reason)[:500],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()
    except Exception as e:
        print(f"⚠️  Could not update topic status: {e}")


def ask_topic_or_script():
    print("What do you want to do?")
    print("  1) Write a new script from a topic")
    print("  2) Use my own script file")
    choice = input("Choice (1/2): ").strip()

    if choice == "1":
        topic_text = input("\nEnter your topic: ").strip()
        if not topic_text:
            print("❌ Topic cannot be empty")
            sys.exit(1)
        return topic_text, None

    elif choice == "2":
        script_path = input("\nPath to your script file: ").strip()
        if not os.path.exists(script_path):
            print(f"❌ File not found: {script_path}")
            sys.exit(1)
        with open(script_path, "r", encoding="utf-8") as f:
            raw_script_text = f.read()
        topic_text = f"[uploaded script: {os.path.basename(script_path)}]"
        return topic_text, raw_script_text

    else:
        print("❌ Invalid choice")
        sys.exit(1)


def ask_style():
    styles = script_engine.list_available_styles()
    if not styles:
        print("❌ No format files found in scriptformat/")
        sys.exit(1)

    print("\nAvailable formats:")
    for i, s in enumerate(styles, start=1):
        print(f"  {i}) {s}")

    choice = input("Pick a number: ").strip()
    try:
        index = int(choice) - 1
        if index < 0 or index >= len(styles):
            raise ValueError
        return styles[index]
    except ValueError:
        print("❌ Invalid selection")
        sys.exit(1)


def ask_duration():
    text = input("\nDuration? (mm:ss, e.g. 5:30): ").strip()
    try:
        return parse_duration(text)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)


def ask_characters():
    text = input("Character names (comma-separated, or press Enter to auto): ").strip()
    return [c.strip() for c in text.split(",") if c.strip()] if text else []


def main():
    print("\n=== MANUAL SCRIPT RUNNER ===\n")

    if run_lock.is_background_running():
        if not handle_conflict():
            return

    topic_text, raw_script_text = ask_topic_or_script()
    style = ask_style()
    duration_secs = ask_duration()
    character_names = ask_characters()

    scene_count = max(1, duration_secs // script_engine.SECONDS_PER_SCENE)

    print(f"\n📝 Topic: {topic_text}")
    print(f"🎨 Style: {style}")
    print(f"⏱️  Duration: {format_duration(duration_secs)} ({scene_count} scenes)")
    if character_names:
        print(f"👤 Characters: {', '.join(character_names)}")

    topic_id = create_manual_topic_row(style, topic_text, duration_secs, character_names)

    try:
        script_engine.run(
            topic=topic_text if raw_script_text is None else None,
            style=style,
            duration_secs=duration_secs,
            scene_count=scene_count,
            character_names=character_names,
            topic_id=topic_id,
            raw_script_text=raw_script_text,
        )
    except (RuntimeError, ValueError) as e:
        print(f"❌ {e}")
        mark_topic_failed(topic_id, e)
        sys.exit(1)

    mark_script_ready(topic_id)

    print("\n✅ Script written. Stopping here — nothing else was run.")
    print("   Review scenes.txt / video_scenes.txt / audio_scenes.txt, then run")
    print("   ref_character_generator.py yourself when you're ready to spend on generation.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")