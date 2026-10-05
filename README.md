
<p align="center">
  <img src="https://github.com/kingdynasty2007-netizen/pov-printer/blob/eda2b30702021783c0ec37df44d4bdf47e6d8391/WhatsApp%20Image%202026-10-02%20at%204.59.49%20PM.jpeg" width="500" alt="POV Printer Logo">
</p>

<h1 align="center">POV PRINTER</h1>
<p align="center"><i>Automated Bible-story video pipeline — script to final cut, with almost no manual steps.</i></p>

<p align="center">
  <img src="https://img.shields.io/badge/Built%20With-Python-blue?style=for-the-badge&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/Status-Active%20Development-orange?style=for-the-badge">
  <img src="https://img.shields.io/badge/ffmpeg-powered-black?style=for-the-badge&logo=ffmpeg&logoColor=white">
</p>

<p align="center">
  <a href="#how-it-works">How It Works</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#tech-stack">Tech Stack</a> •
  <a href="#setup">Setup</a>
</p>

---

### Status
Active development. Core pipeline (story → verification → scene breakdown → image/audio/video generation → assembly) runs end-to-end. Several reliability and scale fixes have been applied based on real multi-hundred-scene runs.

## How It Works

<details>
<summary><b>Click to expand the full 11-step pipeline</b></summary>

1. **Research** — pulls supporting facts/scripture for the topic.
2. **Story Generation** — writes a full-prose story (not scene-by-scene) grounded in the retrieved research.
3. **Story Verification Loop** — an AI quality gate checks hook, pacing, accuracy, and climax. Rewrites until it passes or fails out.
4. **Scene Breakdown** — the approved story is adapted into shot-by-shot scenes — never invented fresh, always derived from the approved story, to prevent plot drift.
5. **Consistency Verification** — checks the scene breakdown didn't drift from the approved story (characters, events, coverage, order, tone).
6. **Reference Generation** — generates persistent reference images for every recurring character, location, and prop, so they stay visually consistent across every scene. One shared style anchor keeps everything in the same art style.
7. **Metadata & Thumbnail** — YouTube title/description/tags and a thumbnail, generated from the approved story.
8. **Image Generation** — per-scene images generated against the character/location/prop references, with AI verification (count, role/position, outfit, style, proportions, era, pose, and face-identity similarity vs. reference).
9. **Audio** — narration via Gemini TTS.
10. **Video** — per-scene video generation with resume-aware retry.
11. **Assembly** — stitches everything into the final video via ffmpeg, falling back to still-image placeholders for any scene that never got a successful video.

</details>

Every generation step has an AI-driven verification step behind it, and failures are automatically retried, escalated, or logged — not silently dropped.

## Architecture

| Module | Role |
|---|---|
| **Script Engine** | Multi-chunk LLM story/script generation, scripture-grounded |
| **Script Verifier** | Format-aware quality gate (PASS / NEEDS_REWRITE / FAILED_QUALITY_GATE) |
| **Reference Generator** | Character/location/prop references, parallelized, shared style anchor |
| **Image Core / Batch Generator** | Scene images across multiple keys, reactive cooldown, auto-retry |
| **Audio Core / Batch Generator** | Narration via Gemini, IPv4-forced |
| **Video Core / Batch Generator** | Cross-key video generation, account-wide rate limiting |
| **Assembly** | ffmpeg stitching with per-scene fallback |
| **Storage Manager** | Per-run SQLite tracking + versioned filesystem storage |
| **Pattern Watcher** | Flags systemic verification failures across a batch |

## Tech Stack

<p align="left">
  <img src="https://img.shields.io/badge/Python%203.12-3776AB?style=flat-square&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/Supabase-3ECF8E?style=flat-square&logo=supabase&logoColor=white">
  <img src="https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white">
  <img src="https://img.shields.io/badge/ffmpeg-black?style=flat-square&logo=ffmpeg&logoColor=white">
  <img src="https://img.shields.io/badge/Gemini%20TTS-4285F4?style=flat-square&logo=googlegemini&logoColor=white">
</p>

- **Agnes API** — image generation, video generation, verification/vision calls
- **OpenRouter / Agnes** — script/story LLM
- **Google Gemini** — narration (TTS)
- **Supabase** — remote data store
- **ImgBB** — image hosting for reference images

## Setup

**1. Install ffmpeg** (not a pip package — install via your OS package manager)
```bash
# Windows
winget install --id=Gyan.FFmpeg -e
Veryfy 
ffmpeg -version
ffprobe -version
# Python environment 
python -m venv venv
venv\Scripts\activate.bat        # Windows
# source venv/bin/activate       # macOS/Linux

python -m pip install --upgrade pip
pip install -r requirements.txt

#env

AGNES_GEN_KEY_1..4
AGNES_VERIFY_KEY_1..5
AGNES_VIDEO_KEY_1..9
AGNES_TEXT_KEY_1..2
GEMINI_KEY_1
IMGBB_API_KEY
OPENROUTER_KEY_1..4
SCRIPT_OPENROUTER_KEY_1
SCRIPT_PROVIDER
SCRIPT_MODEL
SUPABASE_URL
SUPABASE_SERVICE_KEY
