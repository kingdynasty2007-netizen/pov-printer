# ============================================================
# FILE: manual_runner.py
# PURPOSE: Interactive entry point for the manual production pipeline.
#
# Asks the user for topic/script, format, duration, characters.
# Delegates ALL pipeline logic to production_manager.py.
# Checks for a conflicting background run first.
#
# RUN: python manual_runner.py
# RUN (dry-run / safe test): python manual_runner.py --dry-run
# ============================================================

import os
import sys
import time
import argparse
from dotenv import load_dotenv

load_dotenv()

import run_lock
import script_engine
from duration_utils import parse_duration, format_duration
import production_manager as pm
import storage_manager as sm


# ============================================================
# CONFLICT HANDLING
# ============================================================

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


# ============================================================
# INPUT PROMPTS
# ============================================================

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
    text = input("\nDuration? (mm:ss, e.g. 1:00 for 60 seconds): ").strip()
    try:
        return parse_duration(text)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)


def ask_characters():
    text = input("Character names (comma-separated, or press Enter to auto): ").strip()
    return [c.strip() for c in text.split(",") if c.strip()] if text else []


# ============================================================
# SUPABASE TOPIC ROW (optional — graceful if unavailable)
# ============================================================

def create_supabase_topic(style, topic_text, duration_secs, character_names):
    try:
        from db import supabase
        from datetime import datetime, timezone
        response = supabase.table("topics").insert({
            "topic": topic_text,
            "style": style,
            "status": "running",
            "duration_secs": duration_secs,
            "character_names": ",".join(character_names),
        }).execute()
        return response.data[0]["id"]
    except Exception as e:
        print(f"⚠️  Could not save topic to Supabase (continuing without it): {e}")
        return None


def mark_supabase_ready(topic_id):
    if not topic_id:
        return
    try:
        from db import supabase
        from datetime import datetime, timezone
        supabase.table("topics").update({
            "status": "script_ready",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()
    except Exception as e:
        print(f"⚠️  Could not update Supabase topic status: {e}")


def mark_supabase_failed(topic_id, reason):
    if not topic_id:
        return
    try:
        from db import supabase
        from datetime import datetime, timezone
        supabase.table("topics").update({
            "status": "failed",
            "error": str(reason)[:500],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()
    except Exception as e:
        print(f"⚠️  Could not update Supabase topic status: {e}")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="POV Printer — Manual Pipeline")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Safe test mode — no expensive AI/media calls.",
    )
    parser.add_argument(
        "--run-media", action="store_true",
        help="Also run image/audio/video generation after breakdown.",
    )
    parser.add_argument(
        "--topic", type=str, default=None,
        help="Topic (skips interactive prompt, for scripting).",
    )
    parser.add_argument(
        "--format", type=str, default=None, dest="fmt",
        help="Format name (skips interactive prompt).",
    )
    parser.add_argument(
        "--duration", type=str, default=None,
        help="Duration in mm:ss (skips interactive prompt).",
    )
    args = parser.parse_args()

    print("\n=== POV PRINTER — MANUAL PIPELINE ===\n")

    if args.dry_run:
        print("🧪 DRY RUN MODE — no expensive API calls will be made.\n")

    # ---- Conflict check ----
    if run_lock.is_background_running():
        if not handle_conflict():
            return

    # ---- Gather inputs ----
    if args.topic and args.fmt and args.duration:
        # Non-interactive mode (for scripting / tests)
        topic_text = args.topic
        raw_script_text = None
        style = args.fmt
        try:
            duration_secs = parse_duration(args.duration)
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
        character_names = []
    else:
        topic_text, raw_script_text = ask_topic_or_script()
        style = ask_style()
        duration_secs = ask_duration()
        character_names = ask_characters()

    scene_count = max(1, duration_secs // script_engine.SECONDS_PER_SCENE)

    print(f"\n📝 Topic: {topic_text}")
    print(f"🎨 Format: {style}")
    print(f"⏱️  Duration: {format_duration(duration_secs)} ({scene_count} scenes)")
    if character_names:
        print(f"👤 Characters: {', '.join(character_names)}")
    if args.dry_run:
        print("🧪 Mode: DRY RUN")

    # ---- Optional Supabase row ----
    supabase_topic_id = None
    if not args.dry_run:
        supabase_topic_id = create_supabase_topic(
            style, topic_text, duration_secs, character_names
        )

    # ---- Run pipeline ----
    try:
        result = pm.run_manual_pipeline(
            topic=topic_text,
            fmt=style,
            duration_secs=duration_secs,
            character_names=character_names,
            raw_script_text=raw_script_text,
            supabase_topic_id=supabase_topic_id,
            dry_run=args.dry_run,
            run_media=args.run_media,
        )

        if supabase_topic_id:
            mark_supabase_ready(supabase_topic_id)

        print("\n✅ Pipeline complete.")
        print(f"   Run ID: {result['run_id']}")
        print(f"   Root:   {result['paths']['root']}")
        print(f"   Scenes: {result['scene_count']}")
        print("\n   Next steps:")
        if not args.run_media:
            print("   1. Review breakdown/ files")
            print("   2. Run: python ref_character_generator.py")
            print("   3. Run: python run_pipeline.py")

    except RuntimeError as e:
        print(f"\n❌ Pipeline stopped: {e}")
        if supabase_topic_id:
            mark_supabase_failed(supabase_topic_id, e)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")
