# ============================================================
# FILE: assemble_video.py
# For each scene in story order: real video if it succeeded,
# otherwise still-image placeholder. Real narration, or silence
# if narration is missing. Joins into final_video.mp4.
# RUN WITH: python assemble_video.py
# ============================================================

import os
import re
import shutil
import sys

# Settle which run this stage works on BEFORE importing ffmpeg_core, which
# resolves its paths at import time. Bare -> newest run. `... RUN-0009` -> that run.
import run_paths
run_paths.bootstrap_stage(sys.argv)

from ffmpeg_core import (
    load_manifest, image_to_placeholder_video, mux_audio_onto_video,
    generate_silence, concatenate_clips, get_duration,
    ASSEMBLY_FOLDER, FINAL_OUTPUT,
)
from status_board import StatusBoard

IMAGE_SCENES_FILE = run_paths.get_paths()["scenes_file"]


def get_ordered_scene_keys(scenes_file=IMAGE_SCENES_FILE):
    if not os.path.exists(scenes_file):
        print(f"❌ {scenes_file} not found — can't determine scene order.")
        sys.exit(1)

    with open(scenes_file, "r", encoding="utf-8") as f:
        content = f.read()

    raw_blocks = [b.strip() for b in content.split("---") if b.strip()]
    keys = []
    for position, block in enumerate(raw_blocks, start=1):
        frame_match = re.search(r"FRAME\s+0*?(\d+)\s*:", block, re.IGNORECASE)
        number = int(frame_match.group(1)) if frame_match else position
        keys.append(f"scene_{number:03d}")
    return keys


def resolve_video_clip(scene_key, entry, board):
    video_info = entry.get("video", {})
    video_path = video_info.get("local_path")

    if video_info.get("status") == "ok" and video_path and os.path.exists(video_path):
        return video_path, False

    image_path = entry.get("local_path")
    if entry.get("status") == "ok" and image_path and os.path.exists(image_path):
        board.update(scene_key, "no video — building placeholder from image")
        placeholder_path = os.path.join(ASSEMBLY_FOLDER, f"{scene_key}_placeholder.mp4")
        image_to_placeholder_video(image_path, placeholder_path)
        return placeholder_path, True

    return None, False


def resolve_audio_clip(scene_key, entry, clip_duration, board):
    audio_info = entry.get("audio", {})
    audio_path = audio_info.get("local_path")

    if audio_info.get("status") == "ok" and audio_path and os.path.exists(audio_path):
        return audio_path, False

    board.update(scene_key, "no narration — using silence")
    silence_path = os.path.join(ASSEMBLY_FOLDER, f"{scene_key}_silence.aac")
    generate_silence(clip_duration, silence_path)
    return silence_path, True


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("❌ ffmpeg/ffprobe not found on PATH — check your install.")
        sys.exit(1)

    print("\n=== FINAL VIDEO ASSEMBLY ===\n")

    manifest = load_manifest()
    scene_keys = get_ordered_scene_keys()

    if not scene_keys:
        print("❌ No scenes found in scenes.txt.")
        return

    board = StatusBoard(scene_keys)
    board.start()

    clip_paths = []
    skipped = []
    used_placeholder_video = []
    used_silence = []

    for scene_key in scene_keys:
        entry = manifest.get(scene_key)
        if not entry:
            board.update(scene_key, "❌ no manifest entry — skipped", done=True)
            skipped.append(scene_key)
            continue

        board.update(scene_key, "resolving video")
        video_path, is_placeholder = resolve_video_clip(scene_key, entry, board)

        if not video_path:
            board.update(scene_key, "❌ no image or video available — skipped", done=True)
            skipped.append(scene_key)
            continue

        if is_placeholder:
            used_placeholder_video.append(scene_key)

        clip_duration = get_duration(video_path)

        board.update(scene_key, "resolving audio")
        audio_path, is_silent = resolve_audio_clip(scene_key, entry, clip_duration, board)
        if is_silent:
            used_silence.append(scene_key)

        board.update(scene_key, "muxing audio + video")
        output_clip_path = os.path.join(ASSEMBLY_FOLDER, f"{scene_key}_final.mp4")

        try:
            mux_audio_onto_video(video_path, audio_path, output_clip_path)
            clip_paths.append(output_clip_path)
            board.update(scene_key, "✅ ready", done=True)
        except Exception as e:
            board.update(scene_key, f"❌ mux failed: {str(e)[:60]}", done=True)
            skipped.append(scene_key)

    board.stop()

    if not clip_paths:
        print("\n❌ Nothing to assemble — every scene was skipped. Check the errors above.")
        return

    print(f"\n🎬 Joining {len(clip_paths)} clip(s) into {FINAL_OUTPUT}...")
    try:
        concatenate_clips(clip_paths, FINAL_OUTPUT)
    except Exception as e:
        print(f"❌ Final concatenation failed: {e}")
        return

    print(f"\n✅ Done — {FINAL_OUTPUT}")
    print(f"   {len(clip_paths)}/{len(scene_keys)} scenes included")

    if used_placeholder_video:
        print(f"\n⚠️  {len(used_placeholder_video)} scene(s) used a STILL IMAGE (no video): {', '.join(used_placeholder_video)}")
    if used_silence:
        print(f"⚠️  {len(used_silence)} scene(s) have NO NARRATION (silent): {', '.join(used_silence)}")
    if skipped:
        print(f"❌ {len(skipped)} scene(s) skipped entirely: {', '.join(skipped)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopped.")