"""AI DRIVEN DATA AGENT — MCP 서버 (2단계 최소 구현).

도구 3개: list_tables / get_schema / run_query.
get_metric은 아직 없음.
"""

import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "weblog.db"
MAX_ROWS = 1000

mcp = MCPServer("dataagent")


def connect() -> sqlite3.Connection:
    """읽기 전용 연결. 호출마다 새로 연다 (스레드 안전, 연결 비용 무시 가능)."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB 없음: {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@mcp.tool()
def list_tables() -> list[str]:
    """DB의 테이블 목록을 반환한다."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


@lru_cache(maxsize=32)
def _profile(table: str) -> dict:
    # 전 컬럼 결측/카디널리티를 한 번의 풀스캔으로 계산한다. events(660만 행) 기준 실측 약 16초.
    # 데이터가 정적 스냅샷이라 캐시가 안전하다.
    # ponytail: 캐시는 프로세스 메모리. DB를 재적재하면 서버를 재시작해야 한다.
    with connect() as conn:
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not cols:
            raise ValueError(f"테이블 없음: {table}")

        parts = ["COUNT(*)"]
        for c in cols:
            name, decl = c["name"], (c["type"] or "").upper()
            # TEXT 컬럼은 빈 문자열도 결측으로 센다. 숫자 컬럼은 NULL만.
            blank = f" OR \"{name}\" = ''" if "CHAR" in decl or "TEXT" in decl else ""
            parts.append(f'SUM("{name}" IS NULL{blank})')
            parts.append(f'COUNT(DISTINCT "{name}")')

        sql = f'SELECT {", ".join(parts)} FROM "{table}"'
        vals = list(conn.execute(sql).fetchone())

    total = vals[0]
    columns = []
    for i, c in enumerate(cols):
        missing, distinct = vals[1 + i * 2], vals[2 + i * 2]
        columns.append(
            {
                "name": c["name"],
                "type": c["type"] or "",
                "missing_count": missing,
                "missing_rate": round(missing / total, 4) if total else None,
                "distinct_count": distinct,
            }
        )
    return {"table": table, "row_count": total, "columns": columns, "profile_sql": sql}


@mcp.tool()
def get_schema(table: str) -> dict:
    """테이블의 컬럼명·타입·결측률·고유값 수를 반환한다.

    결측 기준: NULL, 그리고 TEXT 컬럼은 빈 문자열도 포함.
    첫 호출은 전체 스캔이라 수십 초 걸릴 수 있고 이후는 캐시된다.
    """
    if table not in list_tables():
        raise ValueError(f"테이블 없음: {table}. 사용 가능: {list_tables()}")
    return _profile(table)


def _strip_leading_comments(sql: str) -> str:
    """앞쪽 주석을 걷어낸다. 분석가가 주석 붙은 쿼리를 그대로 붙여넣는 경우 대응."""
    s = sql.strip()
    while True:
        if s.startswith("--"):
            s = s.split("\n", 1)[1].strip() if "\n" in s else ""
        elif s.startswith("/*") and "*/" in s:
            s = s.split("*/", 1)[1].strip()
        else:
            return s


@mcp.tool()
def run_query(sql: str) -> dict:
    """SELECT 쿼리를 실행한다. 최대 1000행.

    쓰기는 읽기 전용 연결에서 원천 차단된다. 실행 전 EXPLAIN QUERY PLAN으로
    풀스캔 여부를 확인해 warnings에 담는다.
    """
    body = _strip_leading_comments(sql)
    if not re.match(r"(?i)^(select|with)\b", body):
        raise ValueError("SELECT 또는 WITH로 시작하는 조회 쿼리만 실행한다.")

    warnings = []
    with connect() as conn:
        # CLAUDE.md 규칙 5 — 풀스캔이면 경고한다.
        plan = conn.execute(f"EXPLAIN QUERY PLAN {body}").fetchall()
        scans = [
            r["detail"]
            for r in plan
            if r["detail"].strip().startswith("SCAN ") and "USING" not in r["detail"]
        ]
        if scans:
            warnings.append(f"풀스캔 감지 (660만 행, 수십 초 소요 가능): {'; '.join(scans)}")

        # 여러 문장은 sqlite3가 직접 거부한다. LIMIT을 덧붙이지 않고 1001행만 읽어
        # 상한 초과를 판별한다 — 사용자가 쓴 LIMIT/ORDER BY를 건드리지 않기 위함.
        cur = conn.execute(body)
        rows = cur.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in cur.description] if cur.description else []

    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]
        warnings.append(f"결과가 {MAX_ROWS}행으로 잘렸다. 집계하거나 LIMIT을 명시할 것.")

    return {
        "sql": body,
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "warnings": warnings,
        "query_plan": [r["detail"] for r in plan],
    }


def _selfcheck() -> None:
    assert list_tables() == ["events"], list_tables()

    for bad in ["DELETE FROM events", "UPDATE events SET price = 0", "DROP TABLE events"]:
        try:
            run_query(bad)
            raise AssertionError(f"차단 실패: {bad}")
        except ValueError:
            pass

    # 읽기 전용 연결이 실제 방어선인지 (SELECT 접두사 검사를 우회해도 막히는지)
    try:
        with connect() as conn:
            conn.execute("CREATE TABLE t (x)")
        raise AssertionError("읽기 전용 아님")
    except sqlite3.OperationalError:
        pass

    assert _strip_leading_comments("-- note\nSELECT 1").startswith("SELECT")

    r = run_query("SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type")
    assert r["rows"] and not r["truncated"], r

    r = run_query("SELECT user_id FROM events")
    assert r["truncated"] and r["row_count"] == MAX_ROWS, r["row_count"]

    s = get_schema("events")
    assert s["row_count"] == 6610750, s["row_count"]
    cc = next(c for c in s["columns"] if c["name"] == "category_code")
    assert 0.32 < cc["missing_rate"] < 0.33, cc

    try:
        get_schema("nope")
        raise AssertionError("테이블 검증 실패")
    except ValueError:
        pass

    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        mcp.run()
