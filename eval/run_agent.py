"""eval/run_agent.py — 2.5단계 분석 루프.

questions.yaml의 문항을 하나씩, 매번 새 컨텍스트로 Claude API에 넣고
server.py의 도구 4개를 붙여 돌린 뒤 최종 답변을 JSONL로 기록한다.

사용법:
    python eval/run_agent.py --dry-run           # API 호출 없이 예상 요청 수만 출력
    python eval/run_agent.py                     # 전 문항 → eval/responses.jsonl
    python eval/run_agent.py --no-context --out eval/responses_nocontext.jsonl
    python eval/run_agent.py --questions q01,q09
    python eval/run_agent.py --selfcheck         # 컨텍스트 격리 점검 (API 호출 없음)

인증: ANTHROPIC_API_KEY 또는 `ant auth login` 프로필을 SDK가 알아서 찾는다.
"""

import argparse
import importlib.util
import inspect
import json
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "questions.yaml"
CLAUDE_MD = ROOT / "CLAUDE.md"
METRICS = ROOT / "metrics.yaml"
DEFAULT_OUT = ROOT / "eval" / "responses.jsonl"

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
DEFAULT_MAX_TOOL_CALLS = 15
# 도구 결과가 길면(run_query 1000행) 컨텍스트를 잡아먹는다. 넘치면 잘라서 알린다.
TOOL_RESULT_CHAR_LIMIT = 20000


# ── server.py의 도구 ─────────────────────────────────────────────


def load_server():
    """src/server.py를 import한다. MCP 클라이언트를 띄우지 않고 함수를 직접 호출한다
    (@mcp.tool()은 원본 함수를 그대로 돌려준다)."""
    spec = importlib.util.spec_from_file_location("dataagent_server", ROOT / "src" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dataagent_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def build_tools(srv):
    """Anthropic tools 정의. 설명은 server.py의 docstring에서 가져와 표류를 막는다."""
    doc = lambda fn: inspect.getdoc(fn) or ""
    return [
        {
            "name": "list_tables",
            "description": doc(srv.list_tables),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_schema",
            "description": doc(srv.get_schema),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "테이블 이름"},
                    "where": {
                        "type": "string",
                        "description": "부분집합 조건식. 예 \"event_type = 'purchase'\". 생략하면 전체.",
                    },
                },
                "required": ["table"],
            },
        },
        {
            "name": "run_query",
            "description": doc(srv.run_query),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "실행할 SELECT 쿼리"}},
                "required": ["sql"],
            },
        },
        {
            "name": "get_metric",
            "description": doc(srv.get_metric),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "metrics.yaml의 지표 이름"},
                    "period": {
                        "type": "string",
                        "description": "'YYYY-MM-DD' 또는 'YYYY-MM-DD..YYYY-MM-DD'. 생략 시 전체 기간.",
                    },
                    "segment": {
                        "type": "string",
                        "description": "'column=value' 형태의 단일 필터. 예 'brand=samsung'.",
                    },
                },
                "required": ["name"],
            },
        },
    ]


def execute_tool(srv, name, args):
    """(내용 문자열, is_error). 도구 오류는 예외로 던지지 않고 모델에게 돌려준다."""
    fn = getattr(srv, name, None)
    if fn is None:
        return f"알 수 없는 도구: {name}", True
    try:
        result = fn(**args)
    except Exception as e:                     # noqa: BLE001 — 모델이 고쳐 쓰도록 그대로 전달
        return f"{type(e).__name__}: {e}", True
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > TOOL_RESULT_CHAR_LIMIT:
        text = text[:TOOL_RESULT_CHAR_LIMIT] + (
            f"\n…(결과가 길어 {TOOL_RESULT_CHAR_LIMIT}자에서 잘렸다. "
            "집계하거나 LIMIT을 걸어 다시 조회할 것.)"
        )
    return text, False


# ── 시스템 프롬프트 ──────────────────────────────────────────────

OUTPUT_CONTRACT = """\
너는 SQLite에 적재된 이커머스 행동 로그를 조회·분석하는 데이터 분석 에이전트다.
주어진 도구로 직접 데이터를 확인한 뒤 답한다. 추측으로 숫자를 만들지 않는다.

최종 답변은 반드시 아래 JSON 객체 **하나만** 출력한다. 앞뒤에 다른 문장이나
코드펜스를 붙이지 않는다.

{"value": <값>, "text": "<서술>", "refused": <true|false>}

- value: 숫자 하나면 스칼라, 여러 행이면 행의 리스트(예 [["samsung", 21953], ...]).
  계산하지 않았거나 답할 수 없으면 null.
- text: 어떤 기준으로 계산했는지, 어떤 한계가 있는지에 대한 서술. 실행한 SQL을 포함한다.
- refused: 데이터 범위 밖이라 계산하지 않았으면 true, 계산했으면 false.

비율은 반드시 **0~1 스케일**로 낸다. 8.93%는 0.0893으로 적는다. 퍼센트로 적지 않는다.
금액과 건수는 있는 그대로, 반올림하지 않은 값을 낸다.
"""


def build_system(no_context):
    parts = [OUTPUT_CONTRACT]
    if not no_context:
        parts.append("# 작업 규칙 (CLAUDE.md)\n\n" + CLAUDE_MD.read_text(encoding="utf-8"))
        parts.append("# 지표 정의 (metrics.yaml)\n\n" + METRICS.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


# ── 응답 파싱 ────────────────────────────────────────────────────


def parse_final(text):
    """최종 텍스트에서 JSON 객체를 꺼낸다. 실패하면 (None, False)."""
    if not text:
        return None, False
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None, False
    try:
        obj = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None, False
    return (obj, True) if isinstance(obj, dict) else (None, False)


# ── 루프 ─────────────────────────────────────────────────────────


def run_question(client, srv, tools, system, question, qid, max_tool_calls, fallback=True):
    """문항 하나를 새 컨텍스트로 실행한다.

    messages는 이 함수 안에서 만들어지고 밖으로 나가지 않는다. 호출자는 이전 문항의
    대화를 넘길 수 없다 — 문항 간 컨텍스트 누수를 막는 유일한 지점이다.
    """
    messages = [{"role": "user", "content": question}]
    assert len(messages) == 1, "새 컨텍스트로 시작해야 한다"

    used_tools, tool_calls, api_requests = [], 0, 0
    t0 = time.time()
    extra = (
        {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if fallback else {}
    )
    stop_reason, model_used, capped = None, MODEL, False

    while True:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=system,
            tools=tools,
            messages=messages,
            **extra,
        )
        api_requests += 1
        stop_reason = response.stop_reason
        model_used = getattr(response, "model", MODEL)

        if stop_reason == "refusal":           # content를 읽기 전에 확인해야 한다
            break
        if stop_reason != "tool_use":
            break

        blocks = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        results = []
        for b in blocks:
            tool_calls += 1
            if b.name not in used_tools:
                used_tools.append(b.name)
            content, is_error = execute_tool(srv, b.name, dict(b.input))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})   # 결과는 한 메시지에 모아 보낸다

        if tool_calls >= max_tool_calls:
            capped = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"도구 호출 상한({max_tool_calls}회)에 도달했다. "
                        "더 조회하지 말고 지금까지 확인한 것만으로 최종 JSON을 출력하라."
                    ),
                }
            )
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=system,
                messages=messages,             # tools 없이 — 더 호출할 수 없게 한다
                **extra,
            )
            api_requests += 1
            stop_reason = response.stop_reason
            break

    text = ""
    if stop_reason != "refusal":
        text = "\n".join(b.text for b in response.content if b.type == "text")

    parsed, parse_ok = parse_final(text)
    record = {
        "id": qid,
        "value": parsed.get("value") if parse_ok else None,
        "text": parsed.get("text") if parse_ok else text,
        "refused": bool(parsed.get("refused")) if parse_ok else False,
        "tools": used_tools,                   # 모델의 자기 신고가 아니라 루프가 관찰한 값
        "_meta": {
            "model": model_used,
            "api_requests": api_requests,
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            "parse_ok": parse_ok,
            "tool_cap_hit": capped,
            "elapsed_s": round(time.time() - t0, 1),
        },
    }
    return record


# ── 실행 ─────────────────────────────────────────────────────────


def select(questions, only):
    if not only:
        return questions
    want = {s.strip() for s in only.split(",") if s.strip()}
    picked = [q for q in questions if q["id"] in want]
    missing = want - {q["id"] for q in picked}
    if missing:
        raise SystemExit(f"없는 문항: {sorted(missing)}")
    return picked


def main(argv=None):
    ap = argparse.ArgumentParser(description="questions.yaml을 에이전트로 실행")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--questions", help="쉼표로 구분한 문항 id. 예 q01,q09")
    ap.add_argument("--no-context", action="store_true",
                    help="시스템 프롬프트에서 CLAUDE.md와 metrics.yaml을 뺀다 (대조군)")
    ap.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    ap.add_argument("--no-fallback", action="store_true",
                    help="거부 시 서버측 모델 폴백을 끈다 (모델 고정이 필요한 실험용)")
    ap.add_argument("--dry-run", action="store_true", help="API 호출 없이 예상 요청 수만 출력")
    ap.add_argument("--selfcheck", action="store_true", help="컨텍스트 격리 점검 (API 호출 없음)")
    args = ap.parse_args(argv)

    if args.selfcheck:
        return _selfcheck()

    qs = select(yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"], args.questions)
    system = build_system(args.no_context)

    if args.dry_run:
        approx = len(system) // 3          # 한글 혼합 기준 대략치
        print(f"문항 수: {len(qs)}")
        print(f"컨텍스트: {'제외 (--no-context)' if args.no_context else '포함 (CLAUDE.md + metrics.yaml)'}")
        print(f"시스템 프롬프트: {len(system):,}자 ≈ {approx:,} 토큰 (대략)")
        print(f"문항당 API 요청: 최소 1회, 최대 {args.max_tool_calls + 1}회")
        print(f"전체 API 요청: 최소 {len(qs)}회, 최대 {len(qs) * (args.max_tool_calls + 1)}회")
        print(f"출력: {args.out}")
        return 0

    import anthropic                        # dry-run/selfcheck에는 필요 없다

    client = anthropic.Anthropic()
    srv = load_server()
    tools = build_tools(srv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    total_requests = 0
    with out.open("w", encoding="utf-8") as f:
        for q in qs:
            rec = run_question(
                client, srv, tools, system,
                q["question"].strip(), q["id"],
                args.max_tool_calls, fallback=not args.no_fallback,
            )
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()                        # 중간에 끊겨도 여기까지는 남는다
            m = rec["_meta"]
            total_requests += m["api_requests"]
            flags = "".join(
                [" PARSE_FAIL" if not m["parse_ok"] else "", " CAP" if m["tool_cap_hit"] else "",
                 f" {m['stop_reason'].upper()}" if m["stop_reason"] == "refusal" else ""]
            )
            print(f"{q['id']}  요청 {m['api_requests']}  도구 {m['tool_calls']}{tuple(rec['tools'])}"
                  f"  {m['elapsed_s']}s{flags}")
    print(f"\n{len(qs)}문항 완료, API 요청 {total_requests}회 → {out}")
    return 0


# ── 점검 ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, blocks, stop_reason):
        self.content, self.stop_reason, self.model = blocks, stop_reason, "fake"


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeClient:
    """messages를 그대로 기록하는 스텁. 첫 턴은 도구를 부르고, 다음 턴에 JSON을 낸다."""

    def __init__(self):
        self.seen, self.beta = [], self
        self.messages = self

    def create(self, **kw):
        self.seen.append([json.loads(json.dumps(m, default=str)) for m in kw["messages"]])
        if len(kw["messages"]) == 1:
            return _FakeResponse(
                [_Block(type="tool_use", id="t1", name="list_tables", input={})], "tool_use"
            )
        return _FakeResponse(
            [_Block(type="text", text='{"value": 1, "text": "ok", "refused": false}')], "end_turn"
        )


def _selfcheck():
    class _Srv:
        @staticmethod
        def list_tables():
            return ["events"]

    tools = [{"name": "list_tables", "description": "d",
              "input_schema": {"type": "object", "properties": {}, "required": []}}]

    client = _FakeClient()
    r1 = run_question(client, _Srv, tools, "SYS", "첫 번째 질문 ALPHA", "q01", 5, fallback=False)
    r2 = run_question(client, _Srv, tools, "SYS", "두 번째 질문 BETA", "q02", 5, fallback=False)

    # 컨텍스트 격리 — 두 번째 문항의 어떤 요청에도 첫 문항의 흔적이 없어야 한다
    first_calls = client.seen[:2]
    later_calls = client.seen[2:]
    assert any("ALPHA" in json.dumps(c, ensure_ascii=False) for c in first_calls)
    for call in later_calls:
        blob = json.dumps(call, ensure_ascii=False)
        assert "ALPHA" not in blob, f"컨텍스트 누수: {blob[:200]}"
        assert "q01" not in blob
    # 두 번째 문항도 항상 user 메시지 1개로 시작한다
    assert len(later_calls[0]) == 1 and "BETA" in json.dumps(later_calls[0], ensure_ascii=False)

    # 관찰한 도구가 기록된다 (모델 자기 신고 아님)
    assert r1["tools"] == ["list_tables"] and r2["tools"] == ["list_tables"]
    assert r1["value"] == 1 and r1["_meta"]["parse_ok"] and r1["_meta"]["tool_calls"] == 1

    # JSON 파싱 — 코드펜스와 잡텍스트를 견딘다
    assert parse_final('```json\n{"value": 3, "text": "t"}\n```')[0]["value"] == 3
    assert parse_final('설명이 앞에 있다 {"value": null, "refused": true} 뒤에도 있다')[0]["refused"]
    assert parse_final("JSON이 아니다") == (None, False)
    assert parse_final("") == (None, False)

    # 도구 오류는 예외로 새지 않고 문자열로 돌아온다
    class _Boom:
        @staticmethod
        def run_query(sql):
            raise ValueError("SELECT만 실행한다.")

    content, is_error = execute_tool(_Boom, "run_query", {"sql": "DELETE FROM events"})
    assert is_error and "SELECT만" in content
    assert execute_tool(_Boom, "없는도구", {})[1]

    # 긴 결과는 잘린다
    class _Big:
        @staticmethod
        def run_query(sql):
            return {"rows": ["x" * 50000]}

    content, _ = execute_tool(_Big, "run_query", {"sql": "SELECT 1"})
    assert len(content) < TOOL_RESULT_CHAR_LIMIT + 200 and "잘렸다" in content

    # --no-context가 실제로 두 파일을 뺀다
    assert "metrics.yaml" not in build_system(True) and "0~1 스케일" in build_system(True)
    assert "purchase_conversion_rate" in build_system(False)

    print("selfcheck ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
