# ============================================================
# FILE: duration_utils.py
# Every duration a person types or reads goes through here.
# Nowhere else in the project should do its own time math.
# ============================================================

def parse_duration(text):
    """"5:30" or "1:19:45" or bare "330" -> total seconds (int)."""
    text = str(text).strip()

    if ":" not in text:
        if not text.isdigit():
            raise ValueError(f"'{text}' isn't a valid duration. Use mm:ss (e.g. 5:30).")
        return int(text)

    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        raise ValueError(f"'{text}' isn't a valid duration. Use mm:ss (e.g. 5:30).")

    if len(parts) == 2:
        minutes, seconds = parts
        if not (0 <= seconds < 60) or minutes < 0:
            raise ValueError(f"'{text}' isn't valid mm:ss — seconds must be 0-59.")
        return minutes * 60 + seconds
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        if not (0 <= seconds < 60) or not (0 <= minutes < 60) or hours < 0:
            raise ValueError(f"'{text}' isn't valid h:mm:ss.")
        return hours * 3600 + minutes * 60 + seconds
    else:
        raise ValueError(f"'{text}' isn't a valid duration. Use mm:ss (e.g. 5:30).")


def format_duration(total_seconds):
    """Inverse of parse_duration — seconds -> mm:ss for display."""
    total_seconds = int(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"