"""
Run this if a scan ever finishes but fails to save to MongoDB (e.g. a
transient Atlas connection issue) - pushes the local backup it wrote
instead of re-running the whole scan from scratch.

Usage: python3 scripts/recover_scan_backup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.picker import recover_backup

if __name__ == "__main__":
    if recover_backup():
        print("Recovered the backed-up scan into the database.")
    else:
        print("No backup found - nothing to recover.")
