# ============================================================
# FILE: import_bible_direct.py
# PURPOSE: One-time import of the flat kjv.json array (31,102
# {"ari","name","verse"} objects) into Supabase's bible_verses
# table, run directly from Acode's Alpine terminal — no VPS
# involved, since Supabase is reachable from anywhere.
#
# FILL IN YOUR OWN SUPABASE_URL / SUPABASE_SERVICE_KEY BELOW
# (same two values already in pov_printer's .env). This script
# holds a real credential briefly — delete it after one successful
# run, same as any other secret in this project.
# ============================================================

import json
import re
import time
import requests

# ---- FILL THESE IN — same values as pov_printer's .env ----
SUPABASE_URL = "https://zvoxloefunovzhdflzjb.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inp2b3hsb2VmdW5vdnpoZGZsempiIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4OTM5Mzk5OSwiZXhwIjoyMTA0OTY5OTk5fQ.tzHd4lSjiweWLPDzLQ0Us7qrHph5JXaXOBEMPmU5S7w"
# -------------------------------------------------------------

KJV_FILE_PATH = "/sdcard/Download/kjv.json"
TRANSLATION = "KJV"
BATCH_SIZE = 500

REST_URL = f"{SUPABASE_URL}/rest/v1/bible_verses?on_conflict=translation,book,chapter,verse"
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}

NAME_TRIM_PATTERN = re.compile(r"\s+\d+:\d+$")


def parse_entry(entry):
    ari_parts = entry["ari"].split(":")
    book_index = int(ari_parts[0])
    chapter = int(ari_parts[1])
    verse = int(ari_parts[2])

    book_name = NAME_TRIM_PATTERN.sub("", entry["name"]).strip()

    return {
        "translation": TRANSLATION,
        "book": book_name,
        "book_order": book_index + 1,
        "chapter": chapter,
        "verse": verse,
        "text": entry["verse"].strip(),
    }


def upload_batch(batch, batch_number, total_batches):
    for attempt in (1, 2):
        try:
            response = requests.post(REST_URL, headers=HEADERS, json=batch, timeout=30)
            if response.ok:
                print(f"✓ batch {batch_number}/{total_batches} ({len(batch)} verses)")
                return True
            else:
                print(f"⚠️  batch {batch_number}/{total_batches} failed (attempt {attempt}): "
                      f"HTTP {response.status_code} {response.text[:200]}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️  batch {batch_number}/{total_batches} network error (attempt {attempt}): {e}")
        time.sleep(2)
    return False


def main():
    print(f"📖 Loading {KJV_FILE_PATH}...")
    with open(KJV_FILE_PATH, "r", encoding="utf-8") as f:
        raw_entries = json.load(f)

    print(f"✅ Loaded {len(raw_entries)} entries")

    rows = [parse_entry(e) for e in raw_entries]

    print(f"\n📤 Uploading in batches of {BATCH_SIZE}...\n")

    total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
    failed_batches = []

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        batch_number = i // BATCH_SIZE + 1
        success = upload_batch(batch, batch_number, total_batches)
        if not success:
            failed_batches.append(batch_number)

    print(f"\n✅ Done — {total_batches - len(failed_batches)}/{total_batches} batches succeeded")
    if failed_batches:
        print(f"❌ Failed batches: {failed_batches}")
        print("   Rerun the script — it's an upsert, already-imported rows won't duplicate.")
    else:
        print("🎉 Full Bible imported successfully.")


if __name__ == "__main__":
    main()