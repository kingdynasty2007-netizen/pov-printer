# ============================================================
# FILE: script_engine.py
# CHANGE: ref_prompts.json now records which style/format was used
# {"style": "bible_story", "characters": {...}} instead of a flat
# {name: prompt} dict. ref_character_generator.py needs this to
# enforce the correct visual style (this was the root cause of a
# reference character coming out photorealistic instead of cartoon
# for a bible_story-format topic).
# ============================================================

import os
import re
import json
import time
import itertools
import threading
import requests
from dotenv import load_dotenv
from db import supabase
from status_board import StatusBoard

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCRIPTFORMAT_FOLDER = os.path.join(BASE_DIR, "scriptformat")
SCENES_FILE = os.path.join(BASE_DIR, "scenes.txt")
VIDEO_SCENES_FILE = os.path.join(BASE_DIR, "video_scenes.txt")
AUDIO_FILE = os.path.join(BASE_DIR, "audio_scenes.txt")
REF_PROMPTS_FILE = os.path.join(BASE_DIR, "ref_prompts.json")

SCRIPT_TEMPERATURE = 0.4
LLM_TIMEOUT_SECONDS = 180
MAX_NETWORK_RETRIES = 3
MAX_FORMAT_RETRIES = 2

SECONDS_PER_SCENE = 10
CHUNK_SIZE = 15


PROVIDER_CONFIGS = {
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": os.environ.get("SCRIPT_MODEL", "nvidia/nemotron-3.5-lightning:free"),
        "key_prefix": "SCRIPT_OPENROUTER_KEY_",
        "key_label": "SOR",
        "max_output_cap": 8000,
        "disable_reasoning": True,
        "extra_headers": {
            "HTTP-Referer": "https://pov-printer.local",
            "X-Title": "POV Printer Script Engine",
        },
    },
    "agnes": {
        "url": "https://apihub.agnes-ai.com/v1/chat/completions",
        "model": os.environ.get("AGNES_TEXT_MODEL", "agnes-2.5-flash"),
        "key_prefix": "AGNES_TEXT_KEY_",
        "key_label": "ATX",
        "max_output_cap": 16000,
        "disable_reasoning": False,
        "extra_headers": {},
    },
}

SCRIPT_PROVIDER = os.environ.get("SCRIPT_PROVIDER", "openrouter").strip().lower()
if SCRIPT_PROVIDER not in PROVIDER_CONFIGS:
    raise RuntimeError(f"Unknown SCRIPT_PROVIDER '{SCRIPT_PROVIDER}' — must be 'openrouter' or 'agnes'")

_active = PROVIDER_CONFIGS[SCRIPT_PROVIDER]


def _collect_keys(prefix):
    found = {}
    for name, value in os.environ.items():
        match = re.match(rf"^{prefix}(\d+)$", name)
        if match and value:
            found[int(match.group(1))] = value
    return found

_raw_keys = _collect_keys(_active["key_prefix"])
if not _raw_keys:
    raise RuntimeError(f"No {_active['key_prefix']}N keys found in .env for SCRIPT_PROVIDER='{SCRIPT_PROVIDER}'")

SCRIPT_KEYS = {f"{_active['key_label']}{n}": _raw_keys[n] for n in sorted(_raw_keys)}
print(f"🔑 script_engine: provider={SCRIPT_PROVIDER} model={_active['model']} — {len(SCRIPT_KEYS)} key(s): {', '.join(SCRIPT_KEYS)}")

_key_cycle = itertools.cycle(SCRIPT_KEYS.keys())
_key_cycle_lock = threading.Lock()

def _next_lane():
    with _key_cycle_lock:
        return next(_key_cycle)


def list_available_styles():
    if not os.path.exists(SCRIPTFORMAT_FOLDER):
        return []
    return [f[:-4] for f in os.listdir(SCRIPTFORMAT_FOLDER) if f.endswith(".txt")]


def load_format(style):
    filename = f"{style.lower().replace(' ', '_')}.txt"
    path = os.path.join(SCRIPTFORMAT_FOLDER, filename)
    if not os.path.exists(path):
        available = list_available_styles()
        raise RuntimeError(f"No format file for style '{style}'. Available: {', '.join(available)}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def call_llm(prompt, max_tokens, board=None, board_key="script_writing"):
    max_tokens = min(max_tokens, _active["max_output_cap"])
    last_error = "unknown"

    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        lane = _next_lane()
        headers = {
            "Authorization": f"Bearer {SCRIPT_KEYS[lane]}",
            "Content-Type": "application/json",
        }
        headers.update(_active["extra_headers"])

        payload = {
            "model": _active["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": SCRIPT_TEMPERATURE,
            "max_tokens": max_tokens,
        }
        if _active["disable_reasoning"]:
            payload["reasoning"] = {"effort": "none"}

        if board:
            board.update(board_key, f"[{SCRIPT_PROVIDER}] calling {_active['model']} via {lane} (attempt {attempt}/{MAX_NETWORK_RETRIES})")

        try:
            response = requests.post(_active["url"], headers=headers, json=payload, timeout=LLM_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as e:
            last_error = f"[{lane}] network error: {e}"
            continue

        if response.status_code == 429:
            last_error = f"[{lane}] rate limited"
            time.sleep(5)
            continue

        if not response.ok:
            last_error = f"[{lane}] HTTP {response.status_code}: {response.text[:300]}"
            continue

        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            last_error = f"[{lane}] unexpected response shape"
            continue

    raise RuntimeError(f"[{SCRIPT_PROVIDER}] call failed after {MAX_NETWORK_RETRIES} attempt(s): {last_error}")


def _ref_prompts_section():
    return '''First, reference character prompts:

REF_PROMPTS_START
{
  "characters": [
    {
      "name": "character_name",
      "prompt": "Front-facing portrait of [description]. Standing straight, arms relaxed at sides, neutral expression, hands empty, no props. [style-appropriate description]. Plain background. Full body visible."
    }
  ]
}
REF_PROMPTS_END

'''


def _output_rules(scene_count, include_ref_prompts):
    ref_section = _ref_prompts_section() if include_ref_prompts else (
        "Do NOT include a REF_PROMPTS section — characters are already "
        "established from an earlier part of this script.\n\n"
    )

    return f'''============================================================
OUTPUT INSTRUCTIONS — follow this EXACTLY, it is parsed by code:
============================================================
CRITICAL: Do not output any thinking, planning, reasoning, or explanation
text of any kind. Your entire response must consist ONLY of the raw
marked sections below, starting IMMEDIATELY with the first marker.

CRITICAL: You MUST include the closing marker for EVERY section you
open — SCENES_END after the scenes, and AUDIO_END after the narration,
even though AUDIO_END is the very last thing in your response.

CRITICAL: Between every scene in the SCENES section, you MUST place the
exact divider "===SCENE_BREAK===" on its own line — not "---", not any
other symbol. This divider must appear between EVERY pair of consecutive
scenes without exception, or the scenes will be incorrectly merged
together. Double-check before finishing that you have exactly one fewer
divider than the number of scenes you wrote.

{ref_section}Then the scenes:

SCENES_START
CHARACTERS: name1, name2
Scene description. If 2 named characters: name1 must be described as LEFT,
name2 as RIGHT, in that exact order, matching the CHARACTERS: line order.
If 3 named characters: order is LEFT, CENTER, RIGHT, matching CHARACTERS:
line order exactly. NEVER more than 3 named characters in one scene — if
a moment needs a 4th or 5th person, split it into a separate scene instead.
A scene with no named character present (an establishing/object shot) gets
a blank "CHARACTERS:" line.
The character reference image defines identity and clothing ONLY — never
assume a pose from it. State the actual pose/action in every scene's text,
even if it seems obvious.
Do not re-describe appearance already fixed by the reference (hair, build,
clothing color) — only describe action, expression, position, and setting.
One scene = one clear moment. Two sequential actions = two scenes.
===SCENE_BREAK===
CHARACTERS: name1
Next scene description.
===SCENE_BREAK===
SCENES_END

Then the narration:

AUDIO_START
SCENE: scene_001
VOICE: [voice name from the style guide]
Narration text for scene 1.

SCENE: scene_002
VOICE: [voice name]
Narration text for scene 2.
AUDIO_END

RULES:
- Write approximately {scene_count} scenes this response — close is fine,
  it does not need to be the exact number.
- Every scene needs exactly one matching narration block.
- Every image prompt must be self-contained and detailed.
- Every narration line must match what its scene shows.
- No commentary outside the marked sections.
- Do NOT wrap any section (including JSON) in markdown code fences.
- Remember: write SCENES_END and AUDIO_END, and use "===SCENE_BREAK==="
  between every pair of scenes without exception.
'''


def build_first_chunk_prompt(topic, style, format_content, chunk_scene_count,
                              total_scene_count, character_names, raw_script_text=None):
    char_list = ", ".join(character_names) if character_names else "invent appropriate names"

    if raw_script_text:
        source_block = f'''You are breaking an ALREADY-WRITTEN script into shot-by-shot video
production format. Do NOT invent new plot content — adapt the material
below. This is PART 1 of a longer breakdown (about {total_scene_count}
scenes total); write only the FIRST {chunk_scene_count} scenes now,
covering the beginning of the script.

EXISTING SCRIPT:
{raw_script_text}'''
    else:
        source_block = f'''Write a complete NEW script for a short-form video.
This is PART 1 of a longer script (about {total_scene_count} scenes total);
write only the FIRST {chunk_scene_count} scenes now — establish the
opening of the story, it will continue in later parts.

TOPIC: {topic}'''

    return f'''
{source_block}

STYLE: {style}
CHARACTERS: {char_list}

STYLE GUIDE:
{format_content}
{_output_rules(chunk_scene_count, include_ref_prompts=True)}
'''


def build_continuation_prompt(topic, style, format_content, chunk_scene_count,
                               next_scene_number, total_scene_count,
                               character_names, last_scene_text, last_narration,
                               raw_script_text=None):
    char_list = ", ".join(character_names)

    source_note = (
        f"Continue adapting the SAME existing script from where the previous "
        f"part left off (do not repeat earlier content):\n{raw_script_text}\n"
        if raw_script_text else
        f'Continue the SAME story from the topic "{topic}" — do not restart '
        f"or repeat earlier content."
    )

    return f'''
This is a CONTINUATION of a longer script (about {total_scene_count} scenes
total). You are continuing from scene {next_scene_number} onward.
{source_note}

Established characters (use these exact names, do not invent new ones
unless the story clearly needs a new person): {char_list}

STORY SO FAR ENDS WITH:
Last scene: {last_scene_text}
Last narration line: "{last_narration}"

Continue DIRECTLY from this moment — do not repeat it.

STYLE: {style}

STYLE GUIDE:
{format_content}
{_output_rules(chunk_scene_count, include_ref_prompts=False)}
'''


_SECTION_MARKERS = ["REF_PROMPTS_START", "REF_PROMPTS_END", "SCENES_START", "SCENES_END", "AUDIO_START", "AUDIO_END"]


def _extract_section(raw_output, start_marker, end_marker):
    start_idx = raw_output.find(start_marker)
    if start_idx == -1:
        return None
    content_start = start_idx + len(start_marker)

    end_idx = raw_output.find(end_marker, content_start)
    if end_idx != -1:
        return raw_output[content_start:end_idx].strip()

    next_positions = [
        pos for pos in (raw_output.find(m, content_start) for m in _SECTION_MARKERS if m != start_marker)
        if pos != -1
    ]
    if next_positions:
        return raw_output[content_start:min(next_positions)].strip()

    return raw_output[content_start:].strip()


def _extract_json_object(text):
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    return text


def parse_script_output(raw_output, expect_ref_prompts):
    result = {"ref_prompts": {}, "scenes_txt": "", "audio_txt": "", "raw": raw_output, "parse_errors": []}

    if expect_ref_prompts:
        ref_content = _extract_section(raw_output, "REF_PROMPTS_START", "REF_PROMPTS_END")
        if ref_content is not None:
            json_text = _extract_json_object(ref_content)
            try:
                ref_data = json.loads(json_text)
                for char in ref_data.get("characters", []):
                    name = char.get("name", "").lower().strip()
                    prompt = char.get("prompt", "").strip()
                    if name and prompt:
                        result["ref_prompts"][name] = prompt
            except json.JSONDecodeError as e:
                result["parse_errors"].append(f"ref_prompts JSON error: {e}")
        else:
            result["parse_errors"].append("REF_PROMPTS_START marker not found")

    scenes_content = _extract_section(raw_output, "SCENES_START", "SCENES_END")
    if scenes_content is not None:
        result["scenes_txt"] = scenes_content.strip()
    else:
        result["parse_errors"].append("SCENES_START marker not found")

    audio_content = _extract_section(raw_output, "AUDIO_START", "AUDIO_END")
    if audio_content is not None:
        result["audio_txt"] = audio_content.strip()
    else:
        result["parse_errors"].append("AUDIO_START marker not found")

    return result


def _split_scenes(scenes_txt):
    if "===SCENE_BREAK===" in scenes_txt:
        return [b.strip() for b in scenes_txt.split("===SCENE_BREAK===") if b.strip()]
    return [b.strip() for b in scenes_txt.split("---") if b.strip()]


def count_scene_blocks(scenes_txt):
    return len(_split_scenes(scenes_txt))


def _to_file_format(scenes_txt):
    if "===SCENE_BREAK===" in scenes_txt:
        return scenes_txt.replace("===SCENE_BREAK===", "---")
    return scenes_txt


def parse_audio_blocks(audio_txt):
    raw_blocks = re.split(r"SCENE:\s*scene_\d+\s*\n", audio_txt)
    raw_blocks = [b for b in raw_blocks if b.strip()]
    blocks = []
    for block in raw_blocks:
        voice_match = re.match(r"\s*VOICE:\s*(.+)", block)
        if voice_match:
            voice = voice_match.group(1).strip()
            narration = block[voice_match.end():].strip()
        else:
            voice = "Zephyr"
            narration = block.strip()
        blocks.append((voice, narration))
    return blocks


def renumber_audio_blocks(audio_txt, start_number):
    blocks = parse_audio_blocks(audio_txt)
    lines = []
    for i, (voice, narration) in enumerate(blocks):
        scene_number = start_number + i
        lines.append(f"SCENE: scene_{scene_number:03d}")
        lines.append(f"VOICE: {voice}")
        lines.append(narration)
        lines.append("")
    return "\n".join(lines).strip(), len(blocks)


def validate_chunk(parsed):
    hard_issues = []
    soft_issues = []

    if not parsed["scenes_txt"]:
        hard_issues.append("no scenes parsed")
    elif "CHARACTERS:" not in parsed["scenes_txt"]:
        hard_issues.append("scenes output has no CHARACTERS: lines — wrong format")
    if not parsed["audio_txt"]:
        hard_issues.append("no narration parsed")

    if not hard_issues:
        scene_count_found = count_scene_blocks(parsed["scenes_txt"])
        audio_count_found = len(parse_audio_blocks(parsed["audio_txt"]))
        if scene_count_found == 0:
            hard_issues.append("zero scene blocks found")
        elif scene_count_found != audio_count_found:
            soft_issues.append(f"scenes ({scene_count_found}) and narration blocks ({audio_count_found}) don't match")

    if parsed["parse_errors"]:
        hard_issues.append("details: " + "; ".join(parsed["parse_errors"]))

    return hard_issues, soft_issues


def generate_chunk_with_retries(prompt, max_tokens, expect_ref_prompts, board=None, debug_label="chunk"):
    last_error = None
    last_raw = None

    for attempt in range(1, MAX_FORMAT_RETRIES + 1):
        raw_output = call_llm(prompt, max_tokens, board=board)
        last_raw = raw_output
        parsed = parse_script_output(raw_output, expect_ref_prompts)

        hard_issues, soft_issues = validate_chunk(parsed)
        if expect_ref_prompts and not parsed["ref_prompts"]:
            hard_issues.append("no reference character prompts parsed")

        if not hard_issues and not soft_issues:
            return parsed

        if not hard_issues and soft_issues and attempt == MAX_FORMAT_RETRIES:
            print(f"   ⚠️  {debug_label}: accepting with a minor mismatch after {attempt} attempt(s): {'; '.join(soft_issues)}")
            return parsed

        last_error = "; ".join(hard_issues + soft_issues) or "unknown"
        if board:
            board.update("script_writing", f"{debug_label} attempt {attempt}/{MAX_FORMAT_RETRIES} invalid: {last_error}")

    debug_path = os.path.join(BASE_DIR, f"last_failed_{debug_label}.txt")
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(last_raw or "(no output captured)")
        print(f"   🔍 Raw output saved to {debug_path}")
    except Exception:
        pass

    raise RuntimeError(f"{debug_label} failed after {MAX_FORMAT_RETRIES} attempt(s): {last_error}")


def _last_scene_and_narration(scenes_txt, audio_txt):
    scene_blocks = _split_scenes(scenes_txt)
    last_scene = scene_blocks[-1] if scene_blocks else ""
    audio_blocks = parse_audio_blocks(audio_txt)
    last_narration = audio_blocks[-1][1] if audio_blocks else ""
    return last_scene, last_narration


def write_output_files(scenes_txt, audio_txt, ref_prompts, style):
    with open(SCENES_FILE, "w", encoding="utf-8") as f:
        f.write(scenes_txt)
    with open(AUDIO_FILE, "w", encoding="utf-8") as f:
        f.write(audio_txt)
    with open(REF_PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"style": style, "characters": ref_prompts}, f, indent=2)
    print(f"✅ Written: {SCENES_FILE}, {AUDIO_FILE}, {REF_PROMPTS_FILE}")


def save_script_to_db(topic_id, source, duration_secs, scene_count, scenes_txt, audio_txt, ref_prompts):
    if not topic_id:
        return None
    try:
        response = supabase.table("scripts").insert({
            "topic_id": topic_id,
            "source": source,
            "duration_secs": duration_secs,
            "scene_count": scene_count,
            "scenes_txt": scenes_txt,
            "audio_txt": audio_txt,
            "ref_prompts": ref_prompts,
        }).execute()
        script_id = response.data[0]["id"]
        print(f"✅ Script saved to Supabase (id={script_id})")
        return script_id
    except Exception as e:
        print(f"⚠️  Could not save script to Supabase: {e}")
        return None


def run(topic, style, duration_secs, scene_count, character_names,
        topic_id=None, raw_script_text=None):
    board = StatusBoard(["script_writing"])
    board.start()

    try:
        format_content = load_format(style)

        all_scenes_txt = []
        all_audio_txt = []
        ref_prompts = {}
        scenes_written = 0
        next_audio_number = 1
        chunk_index = 0
        max_chunks_safety = (scene_count // 3) + 10

        while scenes_written < scene_count and chunk_index < max_chunks_safety:
            chunk_index += 1
            remaining = scene_count - scenes_written
            requested_size = min(CHUNK_SIZE, remaining)
            max_tokens = min(_active["max_output_cap"], max(2500, 400 + requested_size * 200))

            board.update("script_writing", f"chunk {chunk_index} — requesting ~{requested_size} scenes ({scenes_written}/{scene_count} written so far)")

            if chunk_index == 1:
                prompt = build_first_chunk_prompt(
                    topic, style, format_content, requested_size, scene_count,
                    character_names, raw_script_text=raw_script_text,
                )
                parsed = generate_chunk_with_retries(
                    prompt, max_tokens, expect_ref_prompts=True,
                    board=board, debug_label=f"chunk_{chunk_index}",
                )
                ref_prompts = parsed["ref_prompts"]
                character_names = character_names or list(ref_prompts.keys())
            else:
                last_scene, last_narration = _last_scene_and_narration(all_scenes_txt[-1], all_audio_txt[-1])
                prompt = build_continuation_prompt(
                    topic, style, format_content, requested_size, scenes_written + 1, scene_count,
                    character_names, last_scene, last_narration, raw_script_text=raw_script_text,
                )
                parsed = generate_chunk_with_retries(
                    prompt, max_tokens, expect_ref_prompts=False,
                    board=board, debug_label=f"chunk_{chunk_index}",
                )

            actual_count = count_scene_blocks(parsed["scenes_txt"])
            renumbered_audio, _ = renumber_audio_blocks(parsed["audio_txt"], next_audio_number)

            all_scenes_txt.append(parsed["scenes_txt"])
            all_audio_txt.append(renumbered_audio)
            scenes_written += actual_count
            next_audio_number += actual_count

            board.update("script_writing", f"chunk {chunk_index} complete — {scenes_written}/{scene_count} scenes written")

        final_scenes_txt_raw = "\n===SCENE_BREAK===\n".join(all_scenes_txt)
        final_scenes_txt = _to_file_format(final_scenes_txt_raw)
        final_audio_txt = "\n\n".join(all_audio_txt)

        board.update("script_writing", "writing output files")
        write_output_files(final_scenes_txt, final_audio_txt, ref_prompts, style)

        source = "manual_script" if raw_script_text else "generated"
        if topic_id:
            save_script_to_db(topic_id, source, duration_secs, scenes_written,
                               final_scenes_txt, final_audio_txt, ref_prompts)

        board.update("script_writing", f"✅ complete — {scenes_written} scenes total", done=True)
        return {"scenes_txt": final_scenes_txt, "audio_txt": final_audio_txt, "ref_prompts": ref_prompts}
    finally:
        board.stop()