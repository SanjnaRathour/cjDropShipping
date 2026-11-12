"""
Standalone scan runner - no server needed. Meant to be run once a day by a
cron job (not by keeping the FastAPI app itself running). Writes its result
straight to MongoDB via app.picker.run_scan, same as the app's own "Run new
scan" button, so it shows up in the dashboard next time you open it.

Usage: python3 scripts/run_daily_scan.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import os

from app.cj_client import CJClient
from app.picker import run_scan

CJ_API_KEY = os.getenv("CJ_API_KEY", "")
DESTINATION_COUNTRIES = [c.strip() for c in os.getenv("DESTINATION_COUNTRIES", "IN").split(",") if c.strip()]
MARKUP_MULTIPLIER = float(os.getenv("MARKUP_MULTIPLIER", "3.5"))
CUTENESS_KEYWORDS = [k.strip().lower() for k in os.getenv("CUTENESS_KEYWORDS", "cute,kawaii,pastel").split(",") if k.strip()]

if __name__ == "__main__":
    client = CJClient(api_key=CJ_API_KEY)
    run_scan(client, CUTENESS_KEYWORDS, DESTINATION_COUNTRIES, MARKUP_MULTIPLIER, log=print)
    print("Daily scan complete.")
