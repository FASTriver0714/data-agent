"""AI DRIVEN DATA AGENT — MCP 서버 (2단계 최소 구현).

도구 4개: list_tables / get_schema / run_query / get_metric.
"""

import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "weblog.db"
METRICS_PATH = ROOT / "metrics.yaml"
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
def _profile(table: str, where: str | None) -> dict:
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

        # where는 괄호로 감싼다. 뒤에 절을 덧붙이는 형태의 주입이 구조적으로 문법 오류가 된다.
        clause = f" WHERE ({where})" if where else ""
        sql = f'SELECT {", ".join(parts)} FROM "{table}"{clause}'
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
    return {
        "table": table,
        "where": where,
        "row_count": total,
        "columns": columns,
        "profile_sql": sql,
    }


@mcp.tool()
def get_schema(table: str, where: str | None = None) -> dict:
    """테이블의 컬럼명·타입·결측률·고유값 수를 반환한다.

    where에 조건식을 주면 그 조건을 만족하는 부분집합만 대상으로 계산한다.
    예: get_schema("events", where="event_type = 'purchase'")
    where 없이 호출하면 테이블 전체가 대상이다.

    결측 기준: NULL, 그리고 TEXT 컬럼은 빈 문자열도 포함.
    row_count는 where 적용 후의 행 수이고, 결측률의 분모도 그 값이다.
    첫 호출은 전체 스캔이라 수십 초 걸릴 수 있고 이후 같은 (table, where)는 캐시된다.
    """
    if table not in list_tables():
        raise ValueError(f"테이블 없음: {table}. 사용 가능: {list_tables()}")
    return _profile(table, _check_where(where))


def _check_where(where: str | None) -> str | None:
    """where 조건식 검증. run_query와 같은 수준 — 읽기 전용 연결이 실제 방어선이고,
    여기서는 문장을 덧붙이는 형태만 걸러낸다. 문법 오류는 SQLite가 거부한다."""
    if where is None:
        return None
    body = _strip_leading_comments(where)
    if not body:
        raise ValueError("where가 비어 있다. 조건이 없으면 생략할 것.")
    if ";" in body:
        raise ValueError("where에는 조건식만 넣는다. 세미콜론으로 문장을 붙일 수 없다.")
    return body


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


@lru_cache(maxsize=1)
def _metrics_file() -> dict:
    if not METRICS_PATH.exists():
        raise FileNotFoundError(f"지표 정의 파일 없음: {METRICS_PATH}")
    return yaml.safe_load(METRICS_PATH.read_text(encoding="utf-8"))


# 파생 세그먼트는 컬럼이 아니라 식이다. price_band는 segmentable_by에 있지만 구간 경계가
# 어디에도 정의돼 있지 않아 의도적으로 뺐다 — 임의 구간을 만들면 정의되지 않은 지표를
# 계산하는 셈이 된다(CLAUDE.md 규칙 2).
SEGMENT_EXPR = {
    "category_code": '"category_code"',
    "brand": '"brand"',
    "weekday": "strftime('%w', event_time)",
    "hour": "strftime('%H', event_time)",
}


def _quote(value: str) -> str:
    """SQL 문자열 리터럴. 바인딩 대신 인라인하는 이유는 반환하는 SQL이 그대로
    복사해서 실행 가능해야 하기 때문이다(CLAUDE.md — 분석가가 수정할 수 있어야 한다)."""
    return "'" + str(value).replace("'", "''") + "'"


def _period_filter(period: str | None) -> tuple[str | None, dict, list[str]]:
    """period를 event_time 조건으로 바꾼다. 형식: 'YYYY-MM-DD' 또는 'YYYY-MM-DD..YYYY-MM-DD'."""
    scope = _metrics_file()["data_scope"]
    lo, hi = scope["period_start"], scope["period_end"]
    if period is None:
        return None, {"start": lo, "end": hi, "source": "data_scope (전체 기간)"}, []

    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?", period.strip())
    if not m:
        raise ValueError(
            f"period 형식 오류: {period!r}. "
            "'YYYY-MM-DD' 또는 'YYYY-MM-DD..YYYY-MM-DD'(양끝 포함) 형식만 받는다."
        )
    start = m.group(1) + " 00:00:00"
    end = (m.group(2) or m.group(1)) + " 23:59:59"
    if start > end:
        raise ValueError(f"period의 시작이 끝보다 늦다: {period!r}")

    # 규칙 1 — 데이터 범위 밖은 계산하지 않는다.
    if end < lo or start > hi:
        raise ValueError(
            f"요청 기간({period})이 데이터 범위 밖이다. 보유 범위는 {lo} ~ {hi}이며, "
            "이 범위 밖은 계산하지 않는다."
        )
    warnings = []
    if start < lo or end > hi:
        warnings.append(
            f"요청 기간이 데이터 범위({lo} ~ {hi})를 일부 벗어난다. "
            "겹치는 구간만 집계했으므로 기간 평균으로 읽지 말 것."
        )
    return (
        f"event_time >= {_quote(start)} AND event_time <= {_quote(end)}",
        {"start": start, "end": end, "source": f"period={period}"},
        warnings,
    )


def _segment_filter(segment: str | None, spec: dict, name: str) -> tuple[str | None, str | None]:
    """segment는 'column=value' 형태의 단일 조건이다. 그룹핑이 아니라 필터."""
    if segment is None:
        return None, None
    if "=" not in segment:
        raise ValueError(f"segment 형식 오류: {segment!r}. 'column=value' 형태로 준다.")
    col, value = (s.strip() for s in segment.split("=", 1))

    allowed = spec.get("segmentable_by") or []
    if col not in allowed:
        raise ValueError(f"{name}은 {col}로 세그먼트할 수 없다. 가능한 축: {allowed}")
    if col not in SEGMENT_EXPR:
        raise ValueError(
            f"{col}은 metrics.yaml의 segmentable_by에 있으나 계산 정의가 없다. "
            "구간·기준을 metrics.yaml에 먼저 정의해야 계산한다."
        )

    if col in ("weekday", "hour"):
        limit = 6 if col == "weekday" else 23
        if not value.isdigit() or not 0 <= int(value) <= limit:
            raise ValueError(f"{col} 값은 0~{limit} 정수다. 받은 값: {value!r}")
        value = f"{int(value):02d}" if col == "hour" else str(int(value))

    return f"{SEGMENT_EXPR[col]} = {_quote(value)}", col


@lru_cache(maxsize=64)
def _missing_rate(table: str, column: str) -> float:
    with connect() as conn:
        total, missing = conn.execute(
            f'SELECT COUNT(*), SUM("{column}" IS NULL OR "{column}" = \'\') FROM "{table}"'
        ).fetchone()
    return (missing / total) if total else 0.0


@mcp.tool()
def get_metric(name: str, period: str | None = None, segment: str | None = None) -> dict:
    """metrics.yaml에 정의된 지표를 계산한다. 정의되지 않은 이름은 계산하지 않는다.

    period: 'YYYY-MM-DD' 또는 'YYYY-MM-DD..YYYY-MM-DD'(양끝 포함). 생략하면 전체 기간.
    segment: 'column=value' 형태의 필터. 예 'brand=samsung'. 그룹핑이 아니라 단일 조건이다.

    반환값에 실행 SQL과 caveats가 포함된다. 정의 파일에 없는 이름, not_defined에 걸리는
    이름, 데이터 범위 밖 기간은 계산하지 않고 사유를 반환한다.
    """
    spec_file = _metrics_file()
    metrics = spec_file.get("metrics") or {}
    not_defined = spec_file.get("not_defined") or {}

    if name in not_defined:
        raise ValueError(f"{name}은 정의하지 않기로 한 지표다. 사유: {not_defined[name]['reason'].strip()}")
    if name not in metrics:
        raise ValueError(f"정의되지 않은 지표: {name}. metrics.yaml에 있는 지표: {sorted(metrics)}")

    spec = metrics[name]
    period_cond, period_applied, warnings = _period_filter(period)
    segment_cond, segment_col = _segment_filter(segment, spec, name)

    conds = [c for c in (period_cond, segment_cond) if c]
    filters = "".join(f" AND ({c})" for c in conds)
    sql = spec["sql"].replace("{filters}", filters).strip()

    with connect() as conn:
        row = conn.execute(sql).fetchone()
    value = row[0] if row else None

    # 규칙 4 — 집계 대상 컬럼 결측률이 임계치를 넘으면 명시한다.
    threshold = (spec_file.get("conventions") or {}).get("missing_warning_threshold", 0.10)
    if segment_col in (spec.get("missing_sensitive") or []):
        rate = _missing_rate(spec_file["data_scope"]["table"], segment_col)
        if rate > threshold:
            warnings.append(
                f"{segment_col} 결측률 {rate:.2%} (임계치 {threshold:.0%} 초과). "
                f"결측 행은 이 세그먼트에서 통째로 빠지므로 세그먼트 합이 전체와 일치하지 않는다."
            )
    if value is None:
        warnings.append("분모가 0이라 값이 정의되지 않는다. 기간·세그먼트 조건을 확인할 것.")

    notes = []
    bots = (spec_file.get("exclusions") or {}).get("bot_users") or {}
    if not bots.get("enabled", False):
        notes.append(f"봇 추정 유저 미제외 (기준: {bots.get('rule', 'n/a')}).")

    return {
        "metric": name,
        "label": spec.get("label"),
        "value": value,
        "unit": spec.get("unit"),
        "grain": spec.get("grain"),
        "definition": {
            "numerator": spec.get("numerator"),
            "denominator": spec.get("denominator"),
        },
        "period": period_applied,
        "segment": segment,
        "sql": sql,
        "caveats": spec.get("caveats") or [],
        "warnings": warnings,
        "notes": notes,
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
    assert s["where"] is None, s["where"]
    cc = next(c for c in s["columns"] if c["name"] == "category_code")
    assert 0.32 < cc["missing_rate"] < 0.33, cc

    # where를 주면 부분집합이 분모가 된다. purchase 행은 category_code 결측률이 전체보다 낮다.
    p = get_schema("events", where="event_type = 'purchase'")
    assert p["row_count"] == 106966, p["row_count"]
    pcc = next(c for c in p["columns"] if c["name"] == "category_code")
    assert pcc["missing_count"] == 26119, pcc
    assert round(26119 / 106966, 4) == pcc["missing_rate"], pcc
    assert [c["name"] for c in p["columns"]] == [c["name"] for c in s["columns"]]

    # 조건에 맞는 행이 없어도 0으로 나누지 않는다.
    z = get_schema("events", where="event_type = 'nope'")
    assert z["row_count"] == 0 and z["columns"][0]["missing_rate"] is None, z

    for bad in [
        "",
        "   ",
        "1=1; DROP TABLE events",
        "1=1) UNION SELECT 1, 2 --",  # 괄호 밖으로 나가려 하면 문법 오류가 된다
        "1=1 ORDER BY 1 --",
    ]:
        try:
            get_schema("events", where=bad)
            raise AssertionError(f"where 차단 실패: {bad!r}")
        except (ValueError, sqlite3.OperationalError):
            pass

    # 괄호 안에 갇힌 주입 시도는 조건식으로 남는다. 절을 벗어나지 못한다는 것이 보장 내용.
    contained = get_schema("events", where="1=1) OR (1=1")
    assert contained["row_count"] == 6610750, contained["row_count"]

    try:
        get_schema("nope")
        raise AssertionError("테이블 검증 실패")
    except ValueError:
        pass

    # --- get_metric ---
    m = get_metric("purchase_conversion_rate")
    assert 0 < m["value"] < 1, m["value"]
    assert "{filters}" not in m["sql"] and "WHERE 1 = 1" in m["sql"], m["sql"]
    assert m["caveats"], m
    assert m["period"]["start"] == "2019-10-01 00:00:00", m["period"]

    # metrics.yaml의 SQL 정의대로 계산하면 48.26% (27,217 / 56,396).
    # 같은 caveat이 경고하는 함정 — 분모를 cart 이벤트 총합(124,252)으로 잡으면 86%가 나온다.
    # caveat 본문의 "실제 값은 37% 수준"은 정의 파일 자신의 SQL과 어긋난다. 문서 쪽 확인 필요.
    c = get_metric("cart_to_purchase_rate")
    assert 0.45 < c["value"] < 0.52, c["value"]
    assert c["value"] < 0.80, f"분모를 cart 이벤트 총합으로 잡는 함정에 빠졌다: {c['value']}"

    # period가 좁아지면 분모도 좁아진다. 전체 기간과 같은 SQL이 나오면 필터가 안 먹은 것.
    wk = get_metric("purchase_conversion_rate", period="2019-10-01..2019-10-07")
    assert "2019-10-07 23:59:59" in wk["sql"], wk["sql"]
    assert wk["sql"] != m["sql"]

    # 세그먼트가 결측 민감 컬럼이면 실측 결측률을 경고에 담는다.
    seg = get_metric("purchase_conversion_rate", segment="category_code=electronics.smartphone")
    assert 0 < seg["value"] < 1, seg["value"]
    assert any("category_code 결측률" in w for w in seg["warnings"]), seg["warnings"]

    # 따옴표가 든 값이 조건을 깨지 않는다.
    q = get_metric("purchase_conversion_rate", segment="brand=o'brien")
    assert q["value"] is None and "''" in q["sql"], q

    for bad_name in ["cart_abandonment_rate", "funnel_step_dropoff", "made_up_metric"]:
        try:
            get_metric(bad_name)
            raise AssertionError(f"지표 차단 실패: {bad_name}")
        except ValueError:
            pass

    bad_calls = [
        ("purchase_conversion_rate", {"segment": "price_band=high"}),  # 구간 정의 없음
        ("repeat_purchase_rate", {"segment": "hour=3"}),  # segmentable_by 밖
        ("purchase_conversion_rate", {"segment": "hour=99"}),
        ("purchase_conversion_rate", {"segment": "brand"}),
        ("purchase_conversion_rate", {"period": "2019-09-01"}),  # 데이터 범위 밖
        ("purchase_conversion_rate", {"period": "2019-10-07..2019-10-01"}),
        ("purchase_conversion_rate", {"period": "지난주"}),
    ]
    for metric_name, kwargs in bad_calls:
        try:
            get_metric(metric_name, **kwargs)
            raise AssertionError(f"차단 실패: {metric_name} {kwargs}")
        except ValueError:
            pass

    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        mcp.run()
