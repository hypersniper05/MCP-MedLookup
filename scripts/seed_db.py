"""Seed the SQLite database with medical abbreviations from CSV files."""
import csv
import glob
import os
import sqlite3
import sys

DEFAULT_CSV_DIR = "/tmp/med_abbr/CSVs"
DEFAULT_DB_PATH = os.getenv("DATABASE_PATH", "data/medical.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS abbreviations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    abbreviation TEXT NOT NULL,
    meaning TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'csv',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_abbr_upper ON abbreviations (abbreviation COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS custom_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    definition TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'custom',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_term_upper ON custom_terms (term COLLATE NOCASE);
"""


def seed(csv_dir: str = DEFAULT_CSV_DIR, db_path: str = DEFAULT_DB_PATH):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    csv_files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    if not csv_files:
        print(f"ERROR: No CSV files found in {csv_dir}")
        sys.exit(1)

    total = 0
    for filepath in csv_files:
        rows = []
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                abbr = row[0].strip()
                meaning = row[1].strip()
                # Skip header row
                if abbr == "Abbreviation/Shorthand":
                    continue
                if abbr and meaning:
                    rows.append((abbr, meaning, "csv"))
        conn.executemany(
            "INSERT INTO abbreviations (abbreviation, meaning, source) VALUES (?, ?, ?)",
            rows,
        )
        total += len(rows)
        print(f"  {os.path.basename(filepath)}: {len(rows)} rows")

    conn.commit()
    conn.close()
    print(f"\nSeeded {total} abbreviations into {db_path}")


if __name__ == "__main__":
    csv_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV_DIR
    db_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB_PATH
    seed(csv_dir, db_path)
