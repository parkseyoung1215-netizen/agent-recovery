"""Agent Recovery v0.1 - n8n 실패 실행을 중복 없이 복구하기

n8n 기본 Retry는 실패한 노드를 통째로 다시 실행해서,
  - 응답만 늦었고 실제로는 처리된 요청을 또 보내고 (실험 2)
  - 50건 중 49건이 이미 처리된 노드를 50건 전부 다시 보내요 (실험 3)

이 스크립트는 다시 보내기 전에 항목마다 "이미 처리됐는지" 상대 서비스에 먼저 물어보고,
안 된 것만 중복 방지 키(Idempotency-Key)를 붙여서 보내요.

사용법:
  python3 recover.py <실행ID>          # 복구 계획을 보여주고, y를 입력하면 실행
  python3 recover.py <실행ID> --yes    # 확인 없이 바로 실행
  python3 recover.py --list            # 최근 실패한 실행 목록

필요한 것: 같은 폴더의 .env 파일에  N8N_API_KEY=...  (n8n 설정 > n8n API에서 만든 읽기 전용 키)

v0.1의 한계 (정직하게):
  - 실패한 노드가 HTTP Request 노드인 경우만 다뤄요.
  - 상대 서비스에 "이미 처리됐는지" 조회하는 기능이 있어야 해요. (여기서는 가짜 쇼핑몰의 /check)
  - 실패한 노드 다음에 이어지는 노드가 있으면 실행하지 않고 알려주기만 해요.
  - 요청 본문은 {{ JSON.stringify({ 키: $json.필드, 키: $('노드').item.json.필드 }) }} 형태만 이해해요.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
N8N_URL = os.environ.get("N8N_URL", "http://127.0.0.1:5678")

# 요청 경로 → 조회할 때 쓰는 필드 (가짜 쇼핑몰 기준)
CHECK_FIELD = {"/pay": "order_id", "/inventory": "order_id", "/ship": "order_id", "/email": "to"}


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


def http(method, url, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def n8n(path, key):
    code, data = http("GET", f"{N8N_URL}/api/v1{path}", headers={"X-N8N-API-KEY": key})
    if code != 200:
        sys.exit(f"n8n API 오류 {code}: {data}")
    return data


# ---------- n8n 표현식 아주 작은 해석기 ----------
PAIR = re.compile(r"(\w+)\s*:\s*(\$json\.(\w+)|\$\('([^']+)'\)\.item\.json\.(\w+))")


def build_body(expr, item, index, run_data):
    pairs = PAIR.findall(expr or "")
    if not pairs:
        raise ValueError(f"이 요청 본문 형식은 아직 이해하지 못해요: {expr}")
    body = {}
    for key, _, json_field, node, node_field in pairs:
        if json_field:
            body[key] = item.get(json_field)
        else:
            out = output_items(run_data, node)
            body[key] = (out[index] if index < len(out) else out[0])["json"].get(node_field)
    return body


def output_items(run_data, node):
    runs = run_data.get(node) or []
    for run in reversed(runs):
        main = (run.get("data") or {}).get("main") or []
        if main and main[0]:
            return main[0]
    return []


# ---------- 복구 계획 ----------
def make_plan(execution, workflow):
    rd = execution["data"]["resultData"]
    failed_name = rd.get("lastNodeExecuted")
    nodes = {n["name"]: n for n in workflow["nodes"]}
    node = nodes.get(failed_name)
    if not node or node["type"] != "n8n-nodes-base.httpRequest":
        sys.exit(f"v0.1은 HTTP Request 노드 실패만 다뤄요. 실패한 노드: {failed_name}")

    run = rd["runData"][failed_name][-1]
    parent = ((run.get("source") or [{}])[0] or {}).get("previousNode")
    items = output_items(rd["runData"], parent)
    p = node["parameters"]
    url = p["url"]
    parsed = urllib.parse.urlparse(url)
    field = CHECK_FIELD.get(parsed.path)
    if not field:
        sys.exit(f"이 주소({parsed.path})는 처리 여부를 조회하는 방법을 아직 몰라요.")
    check_base = f"{parsed.scheme}://{parsed.netloc}/check{parsed.path}"

    downstream = [c["node"] for outs in workflow["connections"].get(failed_name, {}).get("main", []) for c in outs]
    plan = []
    for i, it in enumerate(items):
        body = build_body(p.get("jsonBody"), it["json"], i, rd["runData"])
        ident = body.get(field)
        code, res = http("GET", f"{check_base}?{urllib.parse.urlencode({field: ident})}")
        if code != 200:
            status = "확인 불가"
        else:
            status = "이미 처리됨" if res.get("exists") else "다시 보내야 함"
        plan.append({"index": i, "id": ident, "body": body, "status": status,
                     "key": f"{parsed.path.strip('/')}-{ident}"})
    return {"node": failed_name, "url": url, "method": p.get("method", "GET"), "field": field,
            "error": (rd.get("error") or {}).get("message"), "plan": plan, "downstream": downstream}


def show(plan):
    done = [x for x in plan["plan"] if x["status"] == "이미 처리됨"]
    todo = [x for x in plan["plan"] if x["status"] == "다시 보내야 함"]
    unknown = [x for x in plan["plan"] if x["status"] == "확인 불가"]
    print(f"\n실패한 노드: {plan['node']}  ({plan['method']} {plan['url']})")
    print(f"n8n 에러: {plan['error']}")
    print(f"항목 {len(plan['plan'])}개를 하나씩 조회한 결과:")
    print(f"  이미 처리됨     {len(done):>3}개  → 다시 보내지 않음")
    print(f"  다시 보내야 함  {len(todo):>3}개  → 중복 방지 키를 붙여서 보냄")
    if unknown:
        print(f"  확인 불가       {len(unknown):>3}개  → 안전을 위해 보내지 않음 (사람이 확인 필요)")
    for x in todo[:10]:
        print(f"    - {plan['field']}={x['id']}")
    print(f"\n참고: n8n 기본 Retry였다면 {len(plan['plan'])}건을 전부 다시 보냈을 거예요.")
    if plan["downstream"]:
        print(f"주의: 이 노드 다음에 {plan['downstream']} 노드가 있어요. v0.1은 거기까지 이어서 실행하지 않아요.")
    return todo


def run(todo, plan):
    ok = 0
    for x in todo:
        code, res = http(plan["method"], plan["url"], body=x["body"], headers={"Idempotency-Key": x["key"]})
        mark = "OK" if 200 <= code < 300 else f"실패 {code}"
        ok += 200 <= code < 300
        print(f"  {plan['field']}={x['id']} → {mark}")
    print(f"\n복구 완료: {ok}/{len(todo)}건 보냄, 중복으로 보낸 건 0건")


def list_failed(key):
    data = n8n("/executions?status=error&limit=10", key)
    print("최근 실패한 실행:")
    for e in data.get("data", []):
        print(f"  ID {e['id']:>4}  워크플로 {e['workflowId']}  {e.get('stoppedAt', '')}")


def main():
    args = sys.argv[1:]
    key = load_api_key()
    if not args or args[0] == "--list":
        return list_failed(key)
    ex_id = args[0]
    execution = n8n(f"/executions/{ex_id}?includeData=true", key)
    if execution.get("status") != "error":
        sys.exit(f"실행 {ex_id}는 실패한 실행이 아니에요 (상태: {execution.get('status')}).")
    workflow = n8n(f"/workflows/{execution['workflowId']}", key)
    plan = make_plan(execution, workflow)
    todo = show(plan)
    if not todo:
        print("\n다시 보낼 게 없어요. 이 실패는 실제로는 이미 처리된 거예요. 끝!")
        return
    if "--yes" not in args:
        if input("\n위 계획대로 보낼까요? (y/n) ").strip().lower() != "y":
            print("취소했어요. 아무것도 보내지 않았어요.")
            return
    run(todo, plan)


if __name__ == "__main__":
    main()
