# ============================================================
# FILE: batch_video_generator.py
# CHANGE: skips any scene that already has a successful video.
# RUN WITH: python batch_video_generator.py
# ============================================================

from video_core import VIDEO_KEYS, load_manifest, load_video_scene_overrides, get_current_scene_keys, generate_videos
from status_board import StatusBoard


def main():
    print("\n=== BATCH VIDEO GENERATOR ===\n")

    manifest = load_manifest()
    overrides = load_video_scene_overrides()
    current_keys = get_current_scene_keys()

    if not current_keys:
        print("❌ No scenes.txt found (or it's empty).")
        return

    eligible = [
        (key, entry) for key, entry in manifest.items()
        if key in current_keys and entry.get("status") == "ok" and entry.get("url")
    ]

    already_done = [
        (key, entry) for key, entry in eligible
        if entry.get("video", {}).get("status") == "ok"
    ]
    scenes = [
        (key, entry) for key, entry in eligible
        if entry.get("video", {}).get("status") != "ok"
    ]

    if already_done:
        print(f"⏭️  {len(already_done)} scene(s) already have a successful video — skipping")

    skipped_stale = sum(
        1 for key, entry in manifest.items()
        if key not in current_keys and entry.get("status") == "ok"
    )
    if skipped_stale:
        print(f"ℹ️  Ignored {skipped_stale} entr(y/ies) not part of the current scenes.txt")

    if not scenes:
        print("✅ Every eligible scene already has a video. Nothing to do.")
        return

    scene_keys = [key for key, _ in scenes]
    board = StatusBoard(scene_keys)

    print(f"✅ {len(scenes)} scene(s) to process — {len(VIDEO_KEYS)} video key(s): {', '.join(VIDEO_KEYS)}\n")

    board.start()
    results = generate_videos(scenes, board, overrides=overrides)
    board.stop()

    successful = sum(1 for r in results.values() if r["status"] == "ok")
    failed = sum(1 for r in results.values() if r["status"] == "failed")

    print(f"\n✅ Successful: {successful} / {len(scenes)}")
    print(f"❌ Failed: {failed} / {len(scenes)}\n")

    if failed:
        failed_keys = [k for k, r in results.items() if r["status"] == "failed"]
        print(f"Failed scenes: {', '.join(failed_keys)}")
        print("Run: python retry_videos.py --all\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped — progress already saved to manifest.json.")