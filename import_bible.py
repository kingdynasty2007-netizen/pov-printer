# ============================================================
# FILE: import_bible.py
# PURPOSE: One-time bulk import of a public-domain Bible dataset
# (KJV-JSON, one .json file per book) into Supabase.
#
# Run --preview FIRST on one file before importing everything —
# the exact internal JSON key names weren't confirmed by fetching
# the actual file, only inferred from search results. Preview
# catches a wrong assumption before it wastes a full import.
#
# RUN:
#   python import_bible.py --preview bible_data/Genesis.json
#   python import_bible.py --folder bible_data/
# ============================================================

import os
import sys
import json
import argparse
from db import supabase

BOOK_ORDER = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua",
    "Judges", "Ruth", "1 Samuel", "2 Samuel", "1 Kings", "2 Kings",
    "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah", "Esther", "Job",
    "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon", "Isaiah",
    "Jeremiah", "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel",
    "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah",
    "Haggai", "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John",
    "Acts", "Romans", "1 Corinthians", "2 Corinthians", "Galatians",
    "Ephesians", "Philippians", "Colossians", "1 Thessalonians",
    "2 Thessalonians", "1 Timothy", "2 Timothy", "Titus", "Philemon",
    "Hebrews", "James", "1 Peter", "2 Peter", "1 John", "2 John",
    "3 John", "Jude", "Revelation",
]
BOOK_ORDER_MAP = {name: i + 1 for i, name in enumerate(BOOK_ORDER)}


def parse_book_file(path):
    """Defensive parser — tries a couple of common JSON shapes for
    Bible-data repos. Prints exactly what it found so --preview is
    actually useful for spotting a wrong assumption."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    book_name = data.get("book") or data.get("book_name") or os.path.splitext(os.path.basename(path))[0]
    rows = []

    chapters = data.get("chapters")
    if chapters:
        for ch in chapters:
            chapter_num = ch.get("chapter") or ch.get("chapter_number")
            verses = ch.get("verses", [])
            for v in verses:
                verse_num = v.get("verse") or v.get("verse_number")
                text = v.get("text") or v.get("verse_text")
                if chapter_num and verse_num and text:
                    rows.append((book_name, int(chapter_num), int(verse_num), text.strip()))
    else:
        raise RuntimeError(
            f"Unrecognized JSON structure in {path}. Top-level keys found: "
            f"{list(data.keys())}. Paste this file's content back so the "
            f"parser can be adjusted to match."
        )

    return book_name, rows


def preview(path):
    book_name, rows = parse_book_file(path)
    print(f"\n📖 Parsed book: {book_name}")
    print(f"   Total verses found: {len(rows)}")
    if book_name not in BOOK_ORDER_MAP:
        print(f"   ⚠️  '{book_name}' doesn't match any name in BOOK_ORDER — check spelling/format.")
    print("\n   First 3 verses:")
    for book, chapter, verse, text in rows[:3]:
        print(f"   {book} {chapter}:{verse} — {text[:80]}")
    print("\n   Last verse:")
    if rows:
        book, chapter, verse, text = rows[-1]
        print(f"   {book} {chapter}:{verse} — {text[:80]}")
    print("\nIf this looks correct, run the full import with --folder.\n")


def import_folder(folder, translation="KJV", batch_size=500):
    files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    if not files:
        print(f"❌ No .json files found in {folder}")
        sys.exit(1)

    print(f"📚 Found {len(files)} book file(s) in {folder}\n")

    total_inserted = 0
    for filename in files:
        path = os.path.join(folder, filename)
        try:
            book_name, rows = parse_book_file(path)
        except Exception as e:
            print(f"❌ {filename}: {e}")
            continue

        if book_name not in BOOK_ORDER_MAP:
            print(f"⚠️  Skipping '{book_name}' — not found in BOOK_ORDER. Check the name matches exactly.")
            continue

        book_order = BOOK_ORDER_MAP[book_name]
        batch = []
        for book, chapter, verse, text in rows:
            batch.append({
                "translation": translation,
                "book": book,
                "book_order": book_order,
                "chapter": chapter,
                "verse": verse,
                "text": text,
            })
            if len(batch) >= batch_size:
                supabase.table("bible_verses").upsert(batch, on_conflict="translation,book,chapter,verse").execute()
                total_inserted += len(batch)
                batch = []

        if batch:
            supabase.table("bible_verses").upsert(batch, on_conflict="translation,book,chapter,verse").execute()
            total_inserted += len(batch)

        print(f"✓ {book_name}: {len(rows)} verse(s)")

    print(f"\n✅ Import complete — {total_inserted} verse(s) total")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", help="Preview-parse a single book file, no database writes")
    parser.add_argument("--folder", help="Import every .json file in this folder")
    parser.add_argument("--translation", default="KJV")
    args = parser.parse_args()

    if args.preview:
        preview(args.preview)
    elif args.folder:
        import_folder(args.folder, translation=args.translation)
    else:
        print("Usage:")
        print("  python import_bible.py --preview bible_data/Genesis.json")
        print("  python import_bible.py --folder bible_data/")
        sys.exit(1)


if __name__ == "__main__":
    main()