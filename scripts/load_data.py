

import os
import sqlite3
from pathlib import Path

import pandas as pd

# ── 설정 ────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "web_log_data.csv"      
DB = ROOT / "db" / "weblog.db"

CHUNKSIZE = 200_000
LIMIT_ROWS = None                   

# ── 초기화 (멱등성) ──────────────────────────────────────
DB.parent.mkdir(parents=True, exist_ok=True)
if DB.exists():
    os.remove(DB)
    print(f"기존 DB 삭제: {DB.name}")

con = sqlite3.connect(DB)

# ── 적재 ────────────────────────────────────────────────
total = 0
for chunk in pd.read_csv(CSV, chunksize=CHUNKSIZE,
                         dtype={"category_id": str}):
    chunk.to_sql("events", con, if_exists="append", index=False)
    total += len(chunk)
    print(f"{total:,} rows")

    if LIMIT_ROWS and total >= LIMIT_ROWS:
        print(f"LIMIT_ROWS({LIMIT_ROWS:,}) 도달, 중단")
        break

# ── 인덱스 (적재 완료 후) ────────────────────────────────
print("인덱스 생성 중...")
for name, col in [
    ("idx_user", "user_id"),
    ("idx_time", "event_time"),
    ("idx_type", "event_type"),
    ("idx_session", "user_session"),
]:
    con.execute(f"CREATE INDEX {name} ON events({col})")
    print(f"  {name}")

con.commit()
con.close()
print(f"완료: {total:,} rows → {DB}")
import sqlite3, pandas as pd, os

DB = "weblog.db"
if os.path.exists(DB):
    os.remove(DB)          # 항상 처음부터 다시 만든다

con = sqlite3.connect(DB)