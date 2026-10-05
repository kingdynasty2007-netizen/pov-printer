# ============================================================
# FILE: topic_utils.py
# Extracts the #style tag from a raw topic string. Shared by
# topic_queue.py and manual_runner.py so both parse it identically.
# ============================================================

import re

def extract_style_tag(raw_topic):
    """"#pov A day in the life of a chef" -> ("pov", "A day in the life of a chef")
    No #tag present -> (None, original_text) — caller must supply a style."""
    raw_topic = raw_topic.strip()
    match = re.match(r"#(\w+)\s+(.*)", raw_topic)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    return None, raw_topic