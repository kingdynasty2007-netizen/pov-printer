# ============================================================
# FILE: bible_lookup.py
# PURPOSE: Shared search/retrieval functions against the bible_verses
# table. Used by script_engine.py (to ground a new script in real
# scripture) and the validation gate (to check claimed events/
# references against the actual text).
# ============================================================

from db import supabase


def get_verse(book, chapter, verse, translation="KJV"):
    response = (
        supabase.table("bible_verses")
        .select("*")
        .eq("translation", translation)
        .eq("book", book)
        .eq("chapter", chapter)
        .eq("verse", verse)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def get_chapter(book, chapter, translation="KJV"):
    response = (
        supabase.table("bible_verses")
        .select("*")
        .eq("translation", translation)
        .eq("book", book)
        .eq("chapter", chapter)
        .order("verse")
        .execute()
    )
    return response.data


def get_passage(book, chapter, verse_start, verse_end, translation="KJV"):
    response = (
        supabase.table("bible_verses")
        .select("*")
        .eq("translation", translation)
        .eq("book", book)
        .eq("chapter", chapter)
        .gte("verse", verse_start)
        .lte("verse", verse_end)
        .order("verse")
        .execute()
    )
    return response.data


def search_verses(keyword, translation="KJV", limit=15):
    """Full-text keyword search — finds verses containing the given
    word(s). Not semantic/topic search (see build plan's honest
    limitation note) — this matches actual words in the text."""
    response = (
        supabase.table("bible_verses")
        .select("*")
        .eq("translation", translation)
        .text_search("text", keyword)
        .limit(limit)
        .execute()
    )
    return response.data


def get_by_reference(reference_string, translation="KJV"):
    """Parses common formats like '1 Samuel 16:13' or
    '1 Samuel 16:13-16:20' or 'Genesis 1'. Returns a list of verses."""
    import re
    match = re.match(
        r"^(.+?)\s+(\d+):(\d+)(?:-(\d+):(\d+))?$",
        reference_string.strip(),
    )
    if match:
        book, chapter, v_start, _, v_end = match.groups()
        chapter = int(chapter)
        v_start = int(v_start)
        v_end = int(v_end) if v_end else v_start
        return get_passage(book, chapter, v_start, v_end, translation=translation)

    chapter_only = re.match(r"^(.+?)\s+(\d+)$", reference_string.strip())
    if chapter_only:
        book, chapter = chapter_only.groups()
        return get_chapter(book, int(chapter), translation=translation)

    return []