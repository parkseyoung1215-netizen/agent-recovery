"""Agent Recovery 위험 점검기 v0.1 - 실패하기 전에 중복 위험 찾기

n8n의 모든 워크플로를 읽어서, 실패하거나 재시도할 때 같은 일을 두 번 할 수 있는 설정을 찾아요.
점검 규칙은 2026-11-07 실험에서 실제로 중복이 난 경우를 바탕으로 만들었어요.

  [높음] 두 번 하면 안 되는 노드에 자동 재시도(Retry On Fail)가 켜져 있음   → 실험 2b, 3b
  [높음] 두 번 하면 안 되는 노드가 여러 건을 한 번에 처리함                → 실험 3, 3b
  [중간] 두 번 하면 안 되는 HTTP 노드의 시간 제한이 짧음                    → 실험 2
  [중간] 두 번 하면 안 되는 HTTP 노드에 중복 방지 키(Idempotency-Key)가 없음
  [낮음] 실패해도 알려주는 에러 워크플로가 설정돼 있지 않음

사용법:
  python3 check.py            # 모든 워크플로 점검
  python3 check.py <워크플로ID> # 하나만 점검

필요한 것: 같은 폴더의 .env 파일에 N8N_API_KEY=... (읽기 전용 키면 충분, n8n을 바꾸지 않음)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
N8N_URL = os.environ.get("N8N_URL", "http://127.0.0.1:5678")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 보통 "보내기 / 만들기 / 결제" 같은 일을 하는 노드 종류
SIDE_EFFECT_TYPES = {
    "n8n-nodes-base.gmail": "메일 발송",
    "n8n-nodes-base.emailSend": "메일 발송",
    "n8n-nodes-base.slack": "슬랙 메시지",
    "n8n-nodes-base.telegram": "텔레그램 메시지",
    "n8n-nodes-base.discord": "디스코드 메시지",
    "n8n-nodes-base.twilio": "문자 발송",
    "n8n-nodes-base.stripe": "결제",
    "n8n-nodes-base.shopify": "쇼핑몰 주문",
    "n8n-nodes-base.googleSheets": "시트 기록",
    "n8n-nodes-base.airtable": "에어테이블 기록",
    "n8n-nodes-base.notion": "노션 기록",
    "n8n-nodes-base.hubspot": "CRM 기록",
}
READ_OPERATIONS = {"get", "getAll", "list", "search", "read", "lookup"}
SAFE_TIMEOUT_MS = 30000


def load_api_key():
    key = os.environ.get("N8N_API_KEY")
    env = HERE / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("N8N_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("N8N_API_KEY가 없어요. 같은 폴더에 .env 파일을 만들고 N8N_API_KEY=키 를 적어주세요.")
    return key


def n8n(path, key):
    req = urllib.request.Request(f"{N8N_URL}/api/v1{path}", headers={"X-N8N-API-KEY": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"n8n API 오류 {e.code}: {e.read()[:200]!r}")
    except urllib.error.URLError:
        sys.exit("n8n에 연결할 수 없어요. 터미널에서 npx n8n 이 켜져 있는지 확인하세요.")


def all_workflows(key):
    out, cursor = [], None
    while True:
        q = "?limit=100" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        data = n8n(f"/workflows{q}", key)
        out += data.get("data", [])
        cursor = data.get("nextCursor")
        if not cursor:
            return out


def last_item_counts(wf_id, key):
    """가장 최근 실행에서 노드마다 들어온 항목 수. 실행 기록이 없으면 빈 dict."""
    data = n8n(f"/executions?workflowId={wf_id}&limit=1&includeData=true", key).get("data", [])
    if not data:
        return {}
    run_data = ((data[0].get("data") or {}).get("resultData") or {}).get("runData") or {}
    out_count = {}
    for name, runs in run_data.items():
        main = ((runs[-1].get("data") or {}).get("main") or [[]])
        out_count[name] = len(main[0] or []) if main else 0
    counts = {}
    for name, runs in run_data.items():
        src = (runs[-1].get("source") or [None])[0] or {}
        prev = src.get("previousNode")
        if prev in out_count:
            counts[name] = out_count[prev]
    return counts


def side_effect(node):
    """두 번 하면 안 되는 노드면 설명을, 아니면 None."""
    t, p = node["type"], node.get("parameters", {})
    if t == "n8n-nodes-base.httpRequest":
        m = (p.get("method") or "GET").upper()
        return f"HTTP {m}" if m in WRITE_METHODS else None
    if t in SIDE_EFFECT_TYPES:
        if p.get("operation") in READ_OPERATIONS:
            return None
        return SIDE_EFFECT_TYPES[t]
    return None


def has_idempotency_key(node):
    p = node.get("parameters", {})
    if not p.get("sendHeaders"):
        return False
    for h in (p.get("headerParameters") or {}).get("parameters", []):
        if str(h.get("name", "")).lower() == "idempotency-key":
            return True
    return False


def check_workflow(wf, counts):
    findings, no_key = [], []
    for node in wf.get("nodes", []):
        if node.get("disabled"):
            continue
        kind = side_effect(node)
        if not kind:
            continue
        name = node["name"]
        if node.get("retryOnFail"):
            tries = node.get("maxTries", 3)
            findings.append(("높음", name, kind,
                f"자동 재시도가 켜져 있어요 (최대 {tries}번). 응답만 늦어도 같은 요청이 최대 {tries}번 처리될 수 있어요.",
                "자동 재시도를 끄거나, 재시도 전에 '이미 처리됐는지' 확인하는 단계를 넣으세요."))
        n = counts.get(name)
        if n and n > 1:
            findings.append(("높음", name, kind,
                f"최근 실행에서 {n}건을 한 번에 처리했어요. 중간에 1건만 실패해도 Retry하면 {n}건을 전부 다시 보내요.",
                "실패하면 recover.py로 안 된 건만 다시 보내거나, 한 건씩 처리하도록 나누세요."))
        if node["type"] == "n8n-nodes-base.httpRequest":
            timeout = (node.get("parameters", {}).get("options") or {}).get("timeout")
            if timeout and timeout < SAFE_TIMEOUT_MS:
                findings.append(("중간", name, kind,
                    f"시간 제한이 {timeout/1000:g}초예요. 상대가 처리는 했는데 응답만 늦으면 n8n은 실패로 기록해요.",
                    "시간 제한을 늘리거나, 실패 시 recover.py로 '이미 처리됐는지' 먼저 확인하세요."))
            if not has_idempotency_key(node):
                no_key.append(name)
    if no_key:
        findings.append(("중간", ", ".join(no_key), "HTTP 쓰기 요청",
            f"{len(no_key)}개 노드에 중복 방지 키(Idempotency-Key)가 없어요. 같은 요청이 두 번 가면 상대 서비스가 구분할 방법이 없어요.",
            "상대 서비스가 지원하면 헤더에 Idempotency-Key를 넣으세요 (예: 주문번호 기반)."))
    if findings and not (wf.get("settings") or {}).get("errorWorkflow"):
        findings.append(("낮음", "(워크플로 전체)", "-",
            "에러 워크플로가 설정돼 있지 않아요. 실패해도 아무도 모를 수 있어요.",
            "워크플로 설정에서 Error Workflow를 지정해 알림을 받으세요."))
    return findings


ORDER = {"높음": 0, "중간": 1, "낮음": 2}


def main():
    key = load_api_key()
    if len(sys.argv) > 1:
        wfs = [n8n(f"/workflows/{sys.argv[1]}", key)]
    else:
        wfs = all_workflows(key)
    total = {"높음": 0, "중간": 0, "낮음": 0}
    print(f"워크플로 {len(wfs)}개 점검\n")
    for wf in sorted(wfs, key=lambda w: w.get("name", "")):
        findings = check_workflow(wf, last_item_counts(wf["id"], key))
        if not findings:
            print(f"✓ {wf['name']}  (문제 없음)\n")
            continue
        findings.sort(key=lambda f: ORDER[f[0]])
        print(f"■ {wf['name']}  (ID {wf['id']})")
        for level, node, kind, why, fix in findings:
            total[level] += 1
            print(f"  [{level}] {node} - {kind}")
            print(f"         {why}")
            print(f"         → {fix}")
        print()
    print(f"합계: 높음 {total['높음']}개, 중간 {total['중간']}개, 낮음 {total['낮음']}개")
    print("이 점검기는 n8n을 읽기만 하고 아무것도 바꾸지 않았어요.")


if __name__ == "__main__":
    main()
