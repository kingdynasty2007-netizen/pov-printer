# ============================================================
# FILE: run_paths.py
# PURPOSE: ONE place that decides where every pipeline file lives.
#
# Stage scripts no longer need an env var. Running one bare:
#     python batch_image_generator.py
# automatically works on the NEWEST run in data/productions/.
#
# To revisit an older run, pass its id as an argument:
#     python batch_image_generator.py RUN-0009
#
# Resolution order (first match wins):
#   1. a RUN-XXXX argument on the command line
#   2. the POV_RUN_ID env var (production_manager sets this, and its
#      subprocess stages inherit it)
#   3. POV_LEGACY=1 -> old flat folders at the project root. This is
#      what topic_queue.py / run_pipeline.py use, and what preserves
#      the legacy behaviour those two depend on.
#   4. otherwise the newest RUN-XXXX folder on disk
#   5. no runs exist at all -> flat root folders
#
# get_paths() itself stays env-driven so library code is predictable;
# runnable stage scripts call bootstrap_stage() at the very top,
# before importing any core module, because those resolve at import.
# ============================================================

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTIONS_DIR = os.path.join(BASE_DIR, "data", "productions")
RUN_ENV_VAR = "POV_RUN_ID"
LEGACY_ENV_VAR = "POV_LEGACY"

_announced = False


def active_run_id():
    value = os.environ.get(RUN_ENV_VAR, "").strip()
    return value or None


def legacy_mode_requested():
    return os.environ.get(LEGACY_ENV_VAR, "").strip() not in ("", "0", "false", "False")


def latest_run_id():
    """Highest-numbered RUN-XXXX folder that actually exists. This is what
    a stage script falls back to when nothing tells it otherwise, so
    `python batch_image_generator.py` just works on the newest run."""
    if not os.path.isdir(PRODUCTIONS_DIR):
        return None
    best = None
    best_num = -1
    for name in os.listdir(PRODUCTIONS_DIR):
        if not name.startswith("RUN-") or not os.path.isdir(os.path.join(PRODUCTIONS_DIR, name)):
            continue
        try:
            num = int(name[4:])
        except ValueError:
            continue
        if num > best_num:
            best_num, best = num, name
    return best


def run_id_from_argv(argv):
    """`python ref_character_generator.py RUN-0009` -> "RUN-0009".
    Lets someone revisit an old run without setting an env var."""
    for arg in (argv or [])[1:]:
        token = arg.strip()
        if token.startswith("RUN-") and token[4:].isdigit():
            return token
    return None


def resolve_active_run_id(argv=None):
    """Precedence: explicit RUN-id argument > POV_RUN_ID env >
    POV_LEGACY opt-out > newest run on disk > None (flat root folders)."""
    from_arg = run_id_from_argv(argv) if argv is not None else None
    if from_arg:
        return from_arg
    from_env = active_run_id()
    if from_env:
        return from_env
    if legacy_mode_requested():
        return None
    return latest_run_id()


def bootstrap_stage(argv=None):
    """
    Call this at the very top of a runnable stage script, BEFORE importing
    any core module (image_core, audio_core, ffmpeg_core, ...) — those
    resolve their paths at import time, so the run has to be settled first.

    Pins the decision into POV_RUN_ID so every later get_paths() call in the
    same process agrees, instead of re-deriving it.
    """
    resolved = resolve_active_run_id(argv)
    if resolved:
        os.environ[RUN_ENV_VAR] = resolved
    return resolved


def _legacy_paths():
    j = lambda name: os.path.join(BASE_DIR, name)
    return {
        "run_id": None,
        "run_root": BASE_DIR,
        "scenes_file": j("scenes.txt"),
        "audio_scenes_file": j("audio_scenes.txt"),
        "video_scenes_file": j("video_scenes.txt"),
        "frames_file": j("frames.txt"),
        "ref_prompts_file": j("ref_prompts.json"),
        "ref_images_dir": j("ref_images"),
        "ref_characters_file": j("reference_characters.json"),
        "ref_locations_file": j("reference_locations.json"),
        "ref_props_file": j("reference_props.json"),
        "manifest_file": j("manifest.json"),
        "metadata_dir": BASE_DIR,
        "images_dir": j("generated_images"),
        "audio_dir": j("generated_audio"),
        "video_dir": j("generated_videos"),
        "assembly_dir": j("assembly_temp"),
        "final_output": j("final_video.mp4"),
        "assembly_log": j("assembly_log.txt"),
    }


def _run_paths(run_id):
    root = os.path.join(PRODUCTIONS_DIR, run_id)
    if not os.path.isdir(root):
        raise RuntimeError(
            f"{RUN_ENV_VAR}={run_id} but {root} does not exist. Check the run id."
        )
    j = os.path.join
    breakdown = j(root, "breakdown")
    refs = j(root, "references")
    media = j(root, "media")
    final = j(root, "final")
    logs = j(root, "logs")
    return {
        "run_id": run_id,
        "run_root": root,
        "scenes_file": j(breakdown, "scenes.txt"),
        "audio_scenes_file": j(breakdown, "audio_scenes.txt"),
        "video_scenes_file": j(breakdown, "video_scenes.txt"),
        "frames_file": j(breakdown, "frames.txt"),
        "ref_prompts_file": j(refs, "ref_prompts.json"),
        "ref_images_dir": j(refs, "images"),
        "ref_characters_file": j(refs, "reference_characters.json"),
        "ref_locations_file": j(refs, "reference_locations.json"),
        "ref_props_file": j(refs, "reference_props.json"),
        "manifest_file": j(root, "manifest.json"),
        "metadata_dir": j(root, "metadata"),
        "images_dir": j(media, "images"),
        "audio_dir": j(media, "audio"),
        "video_dir": j(media, "video"),
        "assembly_dir": j(final, "assembly_temp"),
        "final_output": j(final, "final_video.mp4"),
        "assembly_log": j(logs, "assembly_log.txt"),
    }


def _ensure_run_dirs(p):
    for key in ("images_dir", "audio_dir", "video_dir", "assembly_dir", "ref_images_dir"):
        os.makedirs(p[key], exist_ok=True)
    for file_key in ("scenes_file", "ref_prompts_file", "final_output", "assembly_log", "metadata_dir"):
        os.makedirs(os.path.dirname(p[file_key]), exist_ok=True)


def get_paths(run_id=None, announce=True):
    """Return every path the pipeline needs. Call this at CALL time in
    in-process modules; import time is fine for subprocess-only stages."""
    global _announced
    run_id = run_id or active_run_id()
    if run_id:
        paths = _run_paths(run_id)
        _ensure_run_dirs(paths)
    else:
        paths = _legacy_paths()

    if announce and not _announced:
        _announced = True
        if run_id:
            print(f"[Run folder: {run_id}  ({paths['run_root']})]")
        else:
            print("[No POV_RUN_ID set - using the legacy flat folders at the project root]")
    return paths