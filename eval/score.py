"""eval/questions.yaml 기준 에이전트 응답 채점기.

사용법:
    python eval/score.py responses.jsonl
    python eval/score.py responses.jsonl --verbose   # 루브릭 문항 응답 전문 출력
    python eval/score.py --selfcheck                 # 비교 로직 자체 점검 (DB·응답 불필요)

응답 파일 형식 (JSONL, 문항당 한 줄, 순서 무관):

    {"id": "q01", "value": 6610750, "text": "...", "tools": ["run_query"]}
    {"id": "q05", "value": [["samsung", 21953], ["apple", 19048]], "tools": ["run_query"]}
    {"id": "q09", "value": 8.93, "text": "...", "tools": ["get_metric"]}
    {"id": "q19", "value": null, "refused": true, "text": "9월 데이터가 없어 계산 불가."}

    id      필수. questions.yaml의 문항 id.
    value   스칼라(숫자) 또는 표(행 리스트). 값을 내지 않았으면 null.
    text    응답 본문. must_mention 검사와 루브릭 수동 확인에 쓴다.
    tools   에이전트가 호출한 도구 이름 목록. 없으면 expect_tool 대조를 건너뛴다.
    refused 거부 문항에서 계산을 거부했는지. 없으면 value로 판단한다.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "questions.yaml"

# tolerance_by_type의 값은 상대오차로 해석한다. questions.yaml이 이전에 두고 있던
# `scalar_tolerance: 1.0e-6 # 비율·금액은 상대오차` 규약을 따른 것이며,
# 절대오차로 바꾸려면 _close 하나만 고치면 된다.
RELATIVE_TOLERANCE = True


# ── 비교 원시 함수 ────────────────────────────────────────────────


def _close(gold, resp, tol):
    """tol=0이면 완전 일치. 그 외에는 상대오차(gold=0이면 절대오차)."""
    if not isinstance(resp, (int, float)) or isinstance(resp, bool):
        return False
    if tol == 0:
        return gold == resp
    if not RELATIVE_TOLERANCE or gold == 0:
        return abs(resp - gold) <= tol
    return abs(resp - gold) / abs(gold) <= tol


def _depercent(gold, resp):
    """gold가 0~1 스케일인데 응답이 퍼센트로 보이면 100으로 나눈다."""
    if isinstance(resp, (int, float)) and not isinstance(resp, bool):
        if abs(gold) <= 1.0 and abs(resp) > 1.0:
            return resp / 100.0
    return resp


def _cell_ok(gold, resp, table_rules):
    """표의 셀 하나를 비교한다. 문자열은 키 컬럼, 숫자는 정수/실수 오차 규칙."""
    nums = table_rules.get("numeric_columns", {}) or {}
    if isinstance(gold, str):
        # key_columns: exact
        return resp == gold, f"키 값 불일치: {gold!r} vs {resp!r}"
    if isinstance(gold, bool):
        return resp == gold, f"불리언 불일치: {gold!r} vs {resp!r}"
    if isinstance(gold, int):
        return _close(gold, resp, nums.get("integer", 0)), f"정수 불일치: {gold} vs {resp}"
    if isinstance(gold, float):
        return _close(gold, resp, nums.get("float", 0.0)), f"실수 불일치: {gold} vs {resp}"
    return resp == gold, f"불일치: {gold!r} vs {resp!r}"


def compare_scalar(gold, resp, value_type, tol_by_type):
    if resp is None:
        return False, "응답에 값이 없다(value: null)"
    if value_type == "ratio":
        resp = _depercent(gold, resp)
    tol = tol_by_type.get(value_type, 0)
    if _close(gold, resp, tol):
        return True, f"{resp} ≈ {gold} (오차 허용 {tol})"
    return False, f"값 불일치: 기대 {gold}, 응답 {resp} (오차 허용 {tol})"


def _percent_columns(gold_rows, resp_rows):
    """퍼센트로 답한 열의 인덱스. 판정은 셀이 아니라 열 단위로 한다.

    같은 열의 값은 같은 단위이므로, 열 안에서 하나라도 1.0을 넘으면 그 열 전체를
    퍼센트로 본다. 셀 단위로 보면 1% 미만 값(0.96 등)은 분수 표기와 구분되지 않아
    같은 열에서도 변환 여부가 갈린다.
    """
    cols = set()
    if not gold_rows:
        return cols

    def cell(rows, i, j):
        row = rows[i]
        return row[j] if isinstance(row, (list, tuple)) and j < len(row) else None

    for j in range(len(gold_rows[0])):
        gvals = [cell(gold_rows, i, j) for i in range(len(gold_rows))]
        if not gvals or not all(isinstance(v, float) and abs(v) <= 1.0 for v in gvals):
            continue        # 0~1 스케일 실수 열만 대상
        rvals = [cell(resp_rows, i, j) for i in range(len(resp_rows))]
        if any(
            isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) > 1.0
            for v in rvals
        ):
            cols.add(j)
    return cols


def compare_table(gold, resp, table_rules):
    if not isinstance(resp, list):
        return False, f"표 형식이 아니다(행 리스트가 필요하다): {type(resp).__name__}"
    if table_rules.get("row_count") == "exact" and len(gold) != len(resp):
        return False, f"행 수 불일치: 기대 {len(gold)}행, 응답 {len(resp)}행"

    g, r = gold, resp
    if not table_rules.get("order_sensitive", True):
        g = sorted(gold, key=lambda row: [str(v) for v in row])
        r = sorted(resp, key=lambda row: [str(v) for v in row])

    pct = _percent_columns(g, r)
    for i, (grow, rrow) in enumerate(zip(g, r)):
        if not isinstance(rrow, (list, tuple)):
            return False, f"{i}행이 리스트가 아니다: {rrow!r}"
        if len(grow) != len(rrow):
            return False, f"{i}행 컬럼 수 불일치: 기대 {len(grow)}, 응답 {len(rrow)}"
        for j, (gv, rv) in enumerate(zip(grow, rrow)):
            if j in pct and isinstance(rv, (int, float)) and not isinstance(rv, bool):
                rv = rv / 100.0
            ok, why = _cell_ok(gv, rv, table_rules)
            if not ok:
                return False, f"{i}행 {j}번째 컬럼 — {why}"
    note = f" (퍼센트로 판정한 열: {sorted(pct)})" if pct else ""
    return True, f"{len(gold)}행 일치{note}"


def check_must_mention(text, terms):
    """must_mention 키워드가 응답 본문에 있는지. 단순 부분문자열 검사다."""
    if not terms:
        return True, []
    body = text or ""
    return ([t for t in terms if t not in body] == []), [t for t in terms if t not in body]


def check_tool(expect, tools):
    """(상태, 사유). tools가 기록되지 않았으면 'skip'."""
    if not expect:
        return "skip", "expect_tool 미지정"
    if tools is None:
        return "skip", "응답에 tools가 기록되지 않아 대조 불가"
    if expect in tools:
        return "ok", f"{expect} 사용"
    return "fail", f"{expect}를 쓰지 않았다 (사용한 도구: {tools or '없음'})"


def route(q):
    """채점 경로 결정.

    rubric 유무로 고르지 않는다. gold_value가 있으면 rubric이 함께 있어도 값 대조로
    채점하고(q15처럼), rubric은 수동 확인 항목으로 덧붙인다.
    """
    if q.get("expect_refusal"):
        return "refusal"
    if q.get("gold_value") is not None:
        return "value"
    return "manual"


# ── 채점 경로 ────────────────────────────────────────────────────


def score_value(q, resp, scoring):
    """경로 1 — gold_value 대조 (q01~q16)."""
    reasons, ok = [], True

    if resp is None:
        return False, ["응답 없음"], "skip"

    gold, vt = q.get("gold_value"), q.get("value_type")
    if gold is None:
        return None, ["gold_value가 비어 있어 자동 채점 불가"], "skip"

    if vt == "table":
        good, why = compare_table(gold, resp.get("value"), scoring.get("table", {}))
    else:
        good, why = compare_scalar(
            gold, resp.get("value"), vt, scoring.get("tolerance_by_type", {})
        )
    ok &= good
    reasons.append(("값 " + ("일치" if good else "불일치")) + f" — {why}")

    mm_ok, missing = check_must_mention(resp.get("text"), q.get("must_mention"))
    if q.get("must_mention"):
        ok &= mm_ok
        reasons.append(
            "must_mention 충족" if mm_ok else f"must_mention 누락: {missing}"
        )

    tstate, twhy = check_tool(q.get("expect_tool"), resp.get("tools"))
    if tstate == "fail":
        ok = False
    reasons.append(f"도구: {twhy}")
    return ok, reasons, tstate


def score_refusal(q, resp, scoring):
    """경로 2 — 거부 판정 (q19·q20).

    기계적으로 확인 가능한 것은 '값을 반환했는가'뿐이다. fail_if의 나머지 조건
    (추정 제시, 대체 계산을 한계 설명보다 앞에 배치 등)은 문장 판단이라 수동 확인 대상이다.
    """
    if resp is None:
        return False, ["응답 없음"], []

    reasons, ok = [], True
    value = resp.get("value")
    if value is not None:
        ok = False
        reasons.append(f"값을 반환했다 → 거부 실패 (value={value!r})")
    else:
        reasons.append("값을 반환하지 않았다")

    if resp.get("refused") is False:
        ok = False
        reasons.append("refused=false — 계산을 시도했다고 기록됨")

    manual = []
    if q.get("fail_if"):
        manual.append(f"fail_if 수동 확인: {q['fail_if'].strip()}")
    if q.get("refusal_reason"):
        manual.append(f"기대 사유: {q['refusal_reason'].strip()}")
    return ok, reasons, manual


def score_rubric(q, resp):
    """경로 3 — 루브릭 (q17·q18). 자동 채점하지 않고 수동 확인 자료만 낸다."""
    lines = [f"rubric ({len(q.get('rubric') or [])}항목):"]
    lines += [f"  [ ] {r}" for r in (q.get("rubric") or [])]
    if q.get("must_mention"):
        _, missing = check_must_mention((resp or {}).get("text"), q["must_mention"])
        lines.append(f"  must_mention {q['must_mention']} → 누락: {missing or '없음'}")
    tstate, twhy = check_tool(q.get("expect_tool"), (resp or {}).get("tools"))
    lines.append(f"  도구: {twhy}")
    return lines


# ── 실행 ─────────────────────────────────────────────────────────


def load_responses(path):
    out = {}
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{lineno} JSON 파싱 실패: {e}")
        if "id" not in obj:
            raise SystemExit(f"{path}:{lineno} id 필드가 없다")
        out[obj["id"]] = obj
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="eval/questions.yaml 기준 응답 채점")
    ap.add_argument("responses", nargs="?", help="응답 JSONL 경로")
    ap.add_argument("--questions", default=str(QUESTIONS))
    ap.add_argument("--verbose", action="store_true", help="루브릭 문항 응답 전문 출력")
    ap.add_argument("--selfcheck", action="store_true", help="비교 로직 점검")
    args = ap.parse_args(argv)

    if args.selfcheck:
        return _selfcheck()
    if not args.responses:
        ap.error("응답 파일 경로가 필요하다 (또는 --selfcheck)")

    doc = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))
    scoring, questions = doc.get("scoring", {}), doc["questions"]
    responses = load_responses(args.responses)

    by_type = {}       # type -> [pass, total]  (자동 채점 대상만)
    auto, manual_ids, tool_skipped, rubric_extra = [], [], [], []

    for q in questions:
        qid, qtype = q["id"], q["type"]
        resp = responses.get(qid)
        print("=" * 72)
        print(f"[{qid}] {qtype} — {' '.join(q['question'].split())}")

        path = route(q)
        if path == "manual":
            print("  판정: MANUAL (gold_value 없음 — 루브릭 수동 확인)")
            for line in score_rubric(q, resp):
                print("  " + line)
            if args.verbose and resp:
                print("  --- 응답 ---")
                for line in (resp.get("text") or "(text 없음)").splitlines():
                    print("  | " + line)
            manual_ids.append(qid)
            continue

        if path == "refusal":
            ok, reasons, extra = score_refusal(q, resp, scoring)
            print(f"  판정: {'PASS' if ok else 'FAIL'}")
            for r in reasons:
                print(f"    - {r}")
            for r in extra:
                print(f"    ~ {r}")
        else:
            ok, reasons, tstate = score_value(q, resp, scoring)
            if ok is None:
                print("  판정: SKIP")
                for r in reasons:
                    print(f"    - {r}")
                continue
            if tstate == "skip":
                tool_skipped.append(qid)
            print(f"  판정: {'PASS' if ok else 'FAIL'}")
            for r in reasons:
                print(f"    - {r}")
            if q.get("rubric"):
                print("  + 값 대조와 별개로 수동 확인:")
                for line in score_rubric(q, resp):
                    print("  " + line)
                rubric_extra.append(qid)

        auto.append((qid, qtype, ok))
        slot = by_type.setdefault(qtype, [0, 0])
        slot[0] += int(bool(ok))
        slot[1] += 1

    print("=" * 72)
    print("유형별 집계")
    for qtype, (p, n) in by_type.items():
        print(f"  {qtype:<8} {p}/{n}")
    passed = sum(1 for _, _, ok in auto if ok)
    print(f"  {'합계':<8} {passed}/{len(auto)}  (자동 채점 대상)")
    print(f"  수동 확인 필요: {manual_ids}")
    if rubric_extra:
        print(f"  값 대조 통과 후 rubric 수동 확인 병행: {rubric_extra}")
    if tool_skipped:
        print(f"  expect_tool 대조 생략(tools 미기록): {tool_skipped}")
    if scoring.get("target"):
        print(f"  목표: {scoring['target']}")
    failed = [qid for qid, _, ok in auto if not ok]
    if failed:
        print(f"  FAIL: {failed}")
    return 0 if not failed else 1


def _selfcheck():
    tol = {"count": 0, "ratio": 0.001, "amount": 0.001, "mean": 0.001}
    table_rules = {
        "row_count": "exact",
        "key_columns": "exact",
        "numeric_columns": {"integer": 0, "float": 0.001},
        "order_sensitive": True,
    }

    # 스칼라 — count는 완전 일치
    assert compare_scalar(6610750, {"value": 6610750}["value"], "count", tol)[0]
    assert not compare_scalar(6610750, 6610749, "count", tol)[0]

    # 비율 — 퍼센트로 답해도 변환 후 비교
    gold_cvr = 0.08934582636257223
    assert compare_scalar(gold_cvr, 8.93, "ratio", tol)[0]
    assert compare_scalar(gold_cvr, 0.0893, "ratio", tol)[0]
    assert not compare_scalar(gold_cvr, 9.5, "ratio", tol)[0]

    # 금액 — 반올림 응답 허용, 자릿수 오류는 불허
    assert compare_scalar(1149.5283308016294, 1149.53, "amount", tol)[0]
    assert not compare_scalar(1149.5283308016294, 11495.28, "amount", tol)[0]

    # 평균 — 건수가 아니므로 반올림 응답을 허용한다 (q11 views_per_session)
    assert compare_scalar(6.820906006793607, 6.821, "mean", tol)[0]
    assert not compare_scalar(6.820906006793607, 6.9, "mean", tol)[0]
    # 같은 값이라도 count로 지정하면 완전 일치라 떨어진다 — q11이 실패했던 원인
    assert not compare_scalar(6.820906006793607, 6.821, "count", tol)[0]

    # 값 없음
    assert not compare_scalar(1.0, None, "amount", tol)[0]

    # 표 — 행 수, 키 컬럼, 순서
    gold = [["samsung", 21953], ["apple", 19048]]
    assert compare_table(gold, [["samsung", 21953], ["apple", 19048]], table_rules)[0]
    assert not compare_table(gold, [["samsung", 21953]], table_rules)[0]           # 행 수
    assert not compare_table(gold, [["apple", 19048], ["samsung", 21953]], table_rules)[0]  # 순서
    assert not compare_table(gold, [["samsung", 21954], ["apple", 19048]], table_rules)[0]  # 정수 오차 0
    assert not compare_table(gold, "표 아님", table_rules)[0]

    # 표 안의 실수 컬럼은 0.001 상대오차 + 퍼센트 변환
    gold_f = [["brand_missing", 1057644, 10197, 0.009641240341740699]]
    assert compare_table(gold_f, [["brand_missing", 1057644, 10197, 0.00964124]], table_rules)[0]
    # 열에 1.0을 넘는 값이 없으면 판단 근거가 없어 변환하지 않는다(한 행뿐인 경우).
    assert not compare_table(gold_f, [["brand_missing", 1057644, 10197, 0.9641240]], table_rules)[0]

    # 열 단위 판정 — q14 실제 값. 1.818이 1.0을 넘으므로 같은 열의 0.964도 함께 변환된다.
    q14 = [
        ["brand_missing", 1057644, 10197, 0.009641240341740699],
        ["brand_present", 5321888, 96769, 0.0181832086657968],
    ]
    assert compare_table(q14, [
        ["brand_missing", 1057644, 10197, 0.9641240],
        ["brand_present", 5321888, 96769, 1.8183209],
    ], table_rules)[0]
    # 분수로 답한 경우도 그대로 통과한다(변환 없음).
    assert compare_table(q14, [
        ["brand_missing", 1057644, 10197, 0.00964124],
        ["brand_present", 5321888, 96769, 0.01818321],
    ], table_rules)[0]
    # 정수 열은 1.0을 넘어도 퍼센트로 보지 않는다.
    assert not compare_table(q14, [
        ["brand_missing", 105764.4, 10197, 0.009641240341740699],
        ["brand_present", 5321888, 96769, 0.0181832086657968],
    ], table_rules)[0]

    # 채점 경로 — rubric이 있어도 gold_value가 있으면 값 대조로 간다(q15)
    assert route({"expect_refusal": True, "gold_value": None}) == "refusal"
    assert route({"gold_value": [["a", 1]], "rubric": ["x"]}) == "value"
    assert route({"rubric": ["x"]}) == "manual"
    assert not compare_table(gold_f, [["brand_missing", 1057644, 10197, 0.0105]], table_rules)[0]

    # order_sensitive=False면 정렬 후 비교
    loose = dict(table_rules, order_sensitive=False)
    assert compare_table(gold, [["apple", 19048], ["samsung", 21953]], loose)[0]

    # must_mention
    assert check_must_mention("요일 간 차이가 미미함을 확인했다", ["요일 간 차이가 미미함"])[0]
    assert not check_must_mention("차이가 컸다", ["요일 간 차이가 미미함"])[0]
    assert check_must_mention("아무 말", [])[0]

    # 도구 대조
    assert check_tool("get_metric", ["get_metric", "run_query"])[0] == "ok"
    assert check_tool("get_metric", ["run_query"])[0] == "fail"
    assert check_tool("get_metric", None)[0] == "skip"

    # 거부 문항
    q19 = {"fail_if": "숫자를 반환하는 경우", "refusal_reason": "단일 월"}
    assert score_refusal(q19, {"value": None, "refused": True}, {})[0]
    assert not score_refusal(q19, {"value": 0.12}, {})[0]
    assert not score_refusal(q19, {"value": None, "refused": False}, {})[0]

    print("selfcheck ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
