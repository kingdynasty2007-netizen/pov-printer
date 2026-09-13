# ============================================================
# FILE: run_pipeline.py
#
# PURPOSE:
# One command runs the whole pipeline unattended:
#
# images
#   → retry-images
#   → video
#   → retry-video
#   → audio
#   → retry-audio
#   → assemble
#
# Each stage runs in its own subprocess.
# Each stage gets its own full log file.
# Terminal shows only short progress messages + spinner.
#
# RUN:
#     python run_pipeline.py
#
# SKIP VIDEO:
#     python run_pipeline.py --no-video
#
# ============================================================

import os
import re
import sys
import json
import time
import signal
import threading
import subprocess
from datetime import datetime


# ============================================================
# CONFIG
# ============================================================

STAGE_LOG_DIR = "pipeline_logs"
SUMMARY_FILE = "pipeline_summary.txt"
MANIFEST_FILE = "manifest.json"
SCENES_FILE = "scenes.txt"

os.makedirs(STAGE_LOG_DIR, exist_ok=True)

PYTHON = sys.executable

RUN_VIDEO_STAGE = "--no-video" not in sys.argv


# ============================================================
# TIMEOUTS
# ============================================================

TIMEOUTS = {
    "images": 4 * 3600,
    "retry_images": 2 * 3600,

    "video": 4 * 3600,
    "retry_video": 2 * 3600,

    "audio": 1.5 * 3600,
    "retry_audio": 1 * 3600,

    "assemble": 30 * 60,
}


# ============================================================
# TERMINAL
# ============================================================

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

spinner_running = False
spinner_thread = None
spinner_lock = threading.Lock()

summary_lines = []


def log(line):
    """
    Print a normal pipeline message and store it
    in the final summary.
    """
    with spinner_lock:
        print("\r" + " " * 100 + "\r", end="")
        print(line)

    summary_lines.append(line)


def spinner_worker(stage_name, start_time):
    """
    Displays a live spinner while a stage subprocess runs.
    """
    index = 0

    while spinner_running:
        elapsed = int(time.time() - start_time)

        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60

        if hours:
            elapsed_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            elapsed_text = f"{minutes:02d}:{seconds:02d}"

        frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]

        with spinner_lock:
            print(
                f"\r{frame} {stage_name} running... [{elapsed_text}]",
                end="",
                flush=True,
            )

        index += 1
        time.sleep(0.12)

    with spinner_lock:
        print("\r" + " " * 100 + "\r", end="", flush=True)


def start_spinner(stage_name, start_time):
    global spinner_running, spinner_thread

    spinner_running = True

    spinner_thread = threading.Thread(
        target=spinner_worker,
        args=(stage_name, start_time),
        daemon=True,
    )

    spinner_thread.start()


def stop_spinner():
    global spinner_running, spinner_thread

    spinner_running = False

    if spinner_thread is not None:
        spinner_thread.join(timeout=1)

    spinner_thread = None


# ============================================================
# SUMMARY
# ============================================================

def write_summary():
    try:
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))

        print(f"\n📝 Full summary saved to {SUMMARY_FILE}")

    except Exception as e:
        print(f"\n⚠️ Could not write summary: {e}")


# ============================================================
# PROCESS CLEANUP
# ============================================================

def kill_process_tree(process):
    """
    Try to terminate the subprocess cleanly.

    On Windows, CREATE_NEW_PROCESS_GROUP allows us to
    target the stage process group.
    """

    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    except Exception:
        try:
            process.kill()
        except Exception:
            pass


# ============================================================
# RUN STAGE
# ============================================================

def run_stage(name, command, timeout_key):
    """
    Run one pipeline stage.

    Full stdout/stderr:
        pipeline_logs/<stage>.log

    Terminal:
        spinner + short status
    """

    log_path = os.path.join(
        STAGE_LOG_DIR,
        f"{name}.log"
    )

    timeout = TIMEOUTS[timeout_key]

    log(
        f"\n▶️  {name} starting"
        f" — log: {log_path}"
    )

    start_time = time.time()

    process = None

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:

            log_file.write(
                f"=== {name} started "
                f"{datetime.now().isoformat()} ===\n\n"
            )

            log_file.flush()

            popen_kwargs = {
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
            }

            # Windows:
            # create a separate process group so we can cleanly
            # terminate a timed-out stage.
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_kwargs["start_new_session"] = True

            process = subprocess.Popen(
                command,
                **popen_kwargs,
            )

            start_spinner(name, start_time)

            try:
                return_code = process.wait(timeout=timeout)

            except subprocess.TimeoutExpired:

                stop_spinner()

                elapsed = int(time.time() - start_time)

                log(
                    f"⏱️  {name} TIMED OUT after "
                    f"{elapsed}s "
                    f"(limit {timeout}s)"
                )

                kill_process_tree(process)

                log(
                    f"💀 {name} process terminated."
                    f" See {log_path}"
                )

                return False

            finally:
                stop_spinner()

        elapsed = int(time.time() - start_time)

        if return_code != 0:

            log(
                f"⚠️  {name} failed "
                f"(exit code {return_code}) "
                f"after {elapsed}s"
            )

            log(
                f"   Full output: {log_path}"
            )

            return False

        log(
            f"✅ {name} finished in {elapsed}s"
        )

        return True

    except FileNotFoundError as e:

        stop_spinner()

        log(
            f"❌ {name} could not start."
        )

        log(
            f"   Command/file not found: {e}"
        )

        log(
            f"   Check that the required script exists."
        )

        return False

    except Exception as e:

        stop_spinner()

        log(
            f"❌ Unexpected error while running "
            f"{name}: {e}"
        )

        return False


# ============================================================
# SCENE PARSER
# ============================================================

def get_current_scene_keys():
    """
    Standalone scene parser.

    Deliberately does NOT import image_core because importing
    image_core may require API keys/reference-image setup.

    Supports:

        FRAME 001:

    and falls back to block position if no FRAME exists.
    """

    if not os.path.exists(SCENES_FILE):
        return set()

    try:
        with open(
            SCENES_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            content = f.read()

    except Exception as e:
        log(f"⚠️ Could not read {SCENES_FILE}: {e}")
        return set()

    raw_blocks = [
        b.strip()
        for b in content.split("---")
        if b.strip()
    ]

    keys = set()

    for position, block in enumerate(
        raw_blocks,
        start=1
    ):

        frame_match = re.search(
            r"FRAME\s+0*?(\d+)\s*:",
            block,
            re.IGNORECASE,
        )

        number = (
            int(frame_match.group(1))
            if frame_match
            else position
        )

        keys.add(
            f"scene_{number:03d}"
        )

    return keys


# ============================================================
# MANIFEST
# ============================================================

def load_manifest():
    """
    Safely load manifest.json.

    A broken/empty manifest returns {} instead of crashing
    the entire pipeline.
    """

    if not os.path.exists(MANIFEST_FILE):
        return {}

    try:
        with open(
            MANIFEST_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            content = f.read().strip()

            if not content:
                return {}

            data = json.loads(content)

            if not isinstance(data, dict):
                log(
                    "⚠️ manifest.json is not a JSON object."
                )
                return {}

            return data

    except json.JSONDecodeError as e:

        log(
            f"❌ manifest.json contains invalid JSON: {e}"
        )

        return {}

    except Exception as e:

        log(
            f"⚠️ Could not read manifest.json: {e}"
        )

        return {}


# ============================================================
# MANIFEST STATUS
# ============================================================

def count_status(
    manifest,
    current_keys,
    field=None,
    status_value="ok",
):

    count = 0

    for key in current_keys:

        entry = manifest.get(key, {})

        if not isinstance(entry, dict):
            continue

        target = (
            entry.get(field, {})
            if field
            else entry
        )

        if not isinstance(target, dict):
            continue

        if target.get("status") == status_value:
            count += 1

    return count


def any_status(
    manifest,
    current_keys,
    status_value,
    field=None,
):

    for key in current_keys:

        entry = manifest.get(key, {})

        if not isinstance(entry, dict):
            continue

        target = (
            entry.get(field, {})
            if field
            else entry
        )

        if not isinstance(target, dict):
            continue

        if target.get("status") == status_value:
            return True

    return False


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():

    log(
        f"=== PIPELINE RUN STARTED "
        f"{datetime.now().isoformat()} ==="
    )

    # --------------------------------------------------------
    # SCENES
    # --------------------------------------------------------

    current_keys = get_current_scene_keys()

    total = len(current_keys)

    if total == 0:

        log(
            "❌ scenes.txt not found or empty. Stopping."
        )

        write_summary()
        return

    log(
        f"📖 Story has {total} scene(s)"
    )

    # --------------------------------------------------------
    # IMAGES
    # --------------------------------------------------------

    run_stage(
        "images",
        [
            PYTHON,
            "batch_image_generator.py",
        ],
        "images",
    )

    manifest = load_manifest()

    if any_status(
        manifest,
        current_keys,
        "failed",
    ):

        run_stage(
            "retry_images",
            [
                PYTHON,
                "retry_images.py",
                "--all",
            ],
            "retry_images",
        )

        manifest = load_manifest()

    image_ok = count_status(
        manifest,
        current_keys,
        status_value="ok",
    )

    log(
        f"📊 Images: {image_ok}/{total} succeeded"
    )

    if image_ok == 0:

        log(
            "\n❌ HARD STOP: zero images succeeded."
        )

        log(
            f"   Check {STAGE_LOG_DIR}/images.log"
        )

        if os.path.exists(
            os.path.join(
                STAGE_LOG_DIR,
                "retry_images.log",
            )
        ):
            log(
                f"   Check {STAGE_LOG_DIR}/retry_images.log"
            )

        write_summary()
        return

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    if RUN_VIDEO_STAGE:

        run_stage(
            "video",
            [
                PYTHON,
                "batch_video_generator.py",
            ],
            "video",
        )

        manifest = load_manifest()

        if any_status(
            manifest,
            current_keys,
            "failed",
            field="video",
        ):

            run_stage(
                "retry_video",
                [
                    PYTHON,
                    "retry_videos.py",
                    "--all",
                ],
                "retry_video",
            )

            manifest = load_manifest()

        video_ok = count_status(
            manifest,
            current_keys,
            field="video",
            status_value="ok",
        )

        log(
            f"📊 Video: {video_ok}/{total} succeeded"
        )

        if video_ok < total:

            log(
                "   ⚠️ Failed video scenes will "
                "fall back to still images during assembly."
            )

    else:

        log(
            "⏭️  Video stage skipped (--no-video)"
        )

        log(
            "   Assembly will use available "
            "still-image content."
        )

    # --------------------------------------------------------
    # AUDIO
    # --------------------------------------------------------

    run_stage(
        "audio",
        [
            PYTHON,
            "batch_audio_generator.py",
        ],
        "audio",
    )

    manifest = load_manifest()

    if any_status(
        manifest,
        current_keys,
        "failed",
        field="audio",
    ):

        run_stage(
            "retry_audio",
            [
                PYTHON,
                "retry_audio.py",
                "--all",
            ],
            "retry_audio",
        )

        manifest = load_manifest()

    audio_ok = count_status(
        manifest,
        current_keys,
        field="audio",
        status_value="ok",
    )

    log(
        f"📊 Audio: {audio_ok}/{total} succeeded"
    )

    if audio_ok < total:

        log(
            "   ⚠️ Missing audio will be silent "
            "during assembly."
        )

    # --------------------------------------------------------
    # ASSEMBLE
    # --------------------------------------------------------

    run_stage(
        "assemble",
        [
            PYTHON,
            "assemble_video.py",
        ],
        "assemble",
    )

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    log(
        f"\n=== PIPELINE RUN FINISHED "
        f"{datetime.now().isoformat()} ==="
    )

    if os.path.exists("final_video.mp4"):

        try:
            size = os.path.getsize(
                "final_video.mp4"
            )

            size_mb = size / (1024 * 1024)

            log(
                f"🎬 final_video.mp4 is ready "
                f"({size_mb:.1f} MB)"
            )

        except Exception:

            log(
                "🎬 final_video.mp4 is ready."
            )

    else:

        log(
            "⚠️ final_video.mp4 was not created."
        )

        log(
            f"   Check {STAGE_LOG_DIR}/assemble.log"
        )

    write_summary()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        stop_spinner()

        log(
            "\n⏹️ Pipeline manually stopped."
        )

        write_summary()

    except Exception as e:

        stop_spinner()

        log(
            f"\n💥 PIPELINE CRASHED: {e}"
        )

        write_summary()

        raise