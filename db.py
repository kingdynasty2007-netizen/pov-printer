# ============================================================
# FILE: db.py
#
# PURPOSE:
#   Thin wrapper around the Supabase Python client.
#   All other scripts import from here — only one place
#   to change if the DB connection details ever change.
#
# USAGE:
#   from db import supabase
#   supabase.table("topics").select("*").execute()
# ============================================================

import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ============================================================
# CONFIG — change these only if you rename your env vars
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# ============================================================
# VALIDATION
# ============================================================

if not SUPABASE_URL:
    raise RuntimeError(
        "❌ SUPABASE_URL not set in .env — "
        "add it before importing db.py"
    )

if not SUPABASE_KEY:
    raise RuntimeError(
        "❌ SUPABASE_SERVICE_KEY not set in .env — "
        "add it before importing db.py"
    )

# ============================================================
# CLIENT — import this everywhere
# ============================================================

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
