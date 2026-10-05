# ============================================================
# FILE: ffmpeg_core.py
# Mux audio onto video (RE-ENCODED to a uniform format — stream
# copy would risk corrupted output when mixing real videos with
# image-to-video placeholder clips). Silent-audio fallback.
# ============================================================

import os
import json
import subprocess
from dotenv import load_dotenv

load_dotenv()
#----updated file path so it will read better ----
import run_paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_P = run_paths.get_paths()
MANIFEST_FILE = _P["manifest_file"]
ASSEMBLY_FOLDER = _P["assembly_dir"]
FINAL_OUTPUT = _P["final_output"]
ASSEMBLY_LOG = _P["assembly_log"]

VIDEO_WIDTH = 1152
VIDEO_HEIGHT = 768
VIDEO_FPS = 24
VIDEO_DURATION = 10

os.makedirs(ASSEMBLY_FOLDER, exist_ok=True)


def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    return {}


def run_ffmpeg(args, description=""):
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error"] + args,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed ({description}): {result.stderr}")


def get_duration(file_path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", file_path],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"Could not read duration of {file_path}: {result.stderr}")


def image_to_placeholder_video(image_path, output_path, duration=VIDEO_DURATION):
    run_ffmpeg([
        "-loop", "1", "-i", image_path,
        "-t", str(duration),
        "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(VIDEO_FPS),
        "-pix_fmt", "yuv420p",
        output_path,
    ], description=f"image→video placeholder ({image_path})")


def generate_silence(duration, output_path):
    run_ffmpeg([
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
        "-t", str(duration),
        "-c:a", "aac",
        output_path,
    ], description="generate silent audio")


def mux_audio_onto_video(video_path, audio_path, output_path):
    video_len = get_duration(video_path)
    audio_len = get_duration(audio_path)

    if audio_len <= 0:
        raise RuntimeError(f"Audio file has zero/invalid duration: {audio_path}")

    video_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={VIDEO_FPS}"
    )
    encode_args = ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-c:a", "aac"]

    if abs(audio_len - video_len) < 0.15:
        run_ffmpeg([
            "-i", video_path, "-i", audio_path,
            "-vf", video_filter,
            *encode_args,
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            output_path,
        ], description="mux (no adjustment)")

    elif audio_len > video_len:
        speed_factor = audio_len / video_len
        run_ffmpeg([
            "-i", video_path, "-i", audio_path,
            "-vf", video_filter,
            "-filter:a", f"atempo={min(speed_factor, 2.0):.4f}",
            *encode_args,
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            output_path,
        ], description="mux (audio sped up to fit)")

    else:
        padding_needed = video_len - audio_len
        padded_audio_path = output_path.rsplit(".", 1)[0] + "_padded_audio.aac"
        run_ffmpeg([
            "-i", audio_path,
            "-af", f"apad=pad_dur={padding_needed:.3f}",
            padded_audio_path,
        ], description="pad audio with silence")

        run_ffmpeg([
            "-i", video_path, "-i", padded_audio_path,
            "-vf", video_filter,
            *encode_args,
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            output_path,
        ], description="mux (audio padded with silence)")


def concatenate_clips(clip_paths, output_path):
    list_file = os.path.join(ASSEMBLY_FOLDER, "concat_list.txt")

    with open(list_file, "w", encoding="utf-8") as f:
        for path in clip_paths:
            safe_path = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        output_path,
    ], description="final concatenation")