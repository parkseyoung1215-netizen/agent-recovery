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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
N8N_URL = os.environ.get("N8N_URL", "http://127.0.0.1:5678")

# 요청 경로 → 조회할 때 쓰는 필드 (가짜 쇼핑몰 기준)
SERVICES_FILE = HERE / "services.json"


def load_services():
    """services.json: 어떤 주소로 보내는 요청을, 어떤 방법으로 '이미 처리됐는지' 확인할지 적어둔 설정"""
    if not SERVICES_FILE.exists():
        return []
    return json.loads(SERVICES_FILE.read_text(encoding="utf-8")).get("services", [])


def find_service(url):
    for svc in load_services():
        match = _env(svc["match"])  # 주소 자체가 비밀이면 ${이름}으로 .env에서 읽음
        if match and url.startswith(match):
            return svc
    return None


def _env(text):
    """설정 안의 ${이름}을 환경변수(.env 포함) 값으로 바꿈. 비밀값을 설정 파일에 적지 않기 위해."""
    def rep(m):
        return os.environ.get(m.group(1)) or read_env_file().get(m.group(1), "")
    return re.sub(r"\$\{(\w+)\}", rep, text)


def read_env_file():
    env = HERE / ".env"
    out = {}
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def lookup(svc, ident):
    """True = 이미 처리됨, False = 없음, None = 확인 불가"""
    if not svc or not svc.get("lookup"):
        return None
    url = _env(svc["lookup"]).replace("{id}", urllib.parse.quote(str(ident), safe=""))
    headers = {k: _env(v) for k, v in (svc.get("headers") or {}).items()}
    code, res = http("GET", url, headers=headers)
    if code != 200:
        return None
    value = res
    for part in svc.get("exists", "exists").split("."):
        value = value.get(part) if isinstance(value, dict) else None
    # true/false가 아니면({}·"false"·null 등) 판단하지 않고 '확인 불가'로 둔다
    return value if isinstance(value, bool) else None


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


def seconds_since(iso):
    from datetime import datetime, timezone
    if not iso:
        return 0
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return 0


# ---------- 다시 보내도 되는지 판단 (v0.3) ----------
# community.n8n.io 피드백 반영:
#  - 타임아웃, 503, 타임아웃 직후의 "없음"은 '아무것도 안 써졌다'는 증거가 아니다.
#  - 늦게 반영되는 조회(eventual)에서는 기다린 뒤의 "없음"도 '아직 안 보임'일 뿐이다.
#  - 그래서 자동으로 다시 보내는 건 두 경우뿐:
#    (a) 조회가 바로 반영되는(strong) 서비스가 "없음"이라고 확인했을 때
#        (타임아웃이었다면 아직 처리 중일 수 있으니 settle_seconds가 지난 뒤)
#    (b) 처음 보낼 때부터 같은 중복 방지 키를 붙였을 때 (서비스가 두 번째를 무시함)
#  - 나머지는 사람에게 넘긴다.
LOOKUP_MODE = os.environ.get("LOOKUP_MODE", "eventual")   # strong: 기록이 즉시 조회됨 / eventual: 늦게 반영될 수 있음
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "30"))


def decide(failure, found, age_seconds, lookup_mode=None, settle=None, keyed_first_attempt=False):
    """항목 하나를 어떻게 할지 정한다.

    failure: "rejected" (상대가 에러 응답을 줌, 예: 503) / "timeout" (응답을 못 받음) / "unknown"
    found:   조회 결과 True(있음) / False(없음) / None(조회 불가·이상한 응답)
    keyed_first_attempt: 처음 요청에도 같은 Idempotency-Key를 붙였는가
    """
    lookup_mode = lookup_mode or LOOKUP_MODE
    settle = SETTLE_SECONDS if settle is None else settle
    if found is True:
        return "이미 처리됨"
    if keyed_first_attempt:
        return "다시 보내야 함"          # 서비스가 같은 키의 두 번째 요청을 무시해 줌
    if found is None:
        return "확인 불가"
    # 여기부터 조회 결과가 '없음'
    if lookup_mode == "strong":
        if failure == "rejected" or age_seconds >= settle:
            return "다시 보내야 함"
        return "확인 대기"              # 타임아웃 직후엔 요청이 아직 처리 중일 수 있음
    # eventual: '없음'은 '아직 안 보임'일 수 있음
    if age_seconds < settle:
        return "확인 대기"
    return "확인 불가"                  # 기다려도 확신할 수 없음 → 사람에게


# ---------- 보내기 전 기록과 잠금 (체크포인트) ----------
# 같은 항목을 두 작업자가 동시에 보내지 않도록, 보내기 '전에' 파일을 원자적으로 만들어 잡는다.
# 보낸 뒤 기록하기 전에 멈추면 기록이 'sending'으로 남고, 다음에는 '확인 불가'로 사람에게 넘긴다.
STATE_DIR = HERE / ".ar_state" / "claims"


def _claim_path(job_key):
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", job_key)
    return STATE_DIR / f"{safe}.json"


def claim(job_key, worker="cli"):
    """이 항목을 내가 처리하겠다고 잡는다. 이미 누가 잡았거나 처리 기록이 있으면 False."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(_claim_path(job_key), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump({"job": job_key, "worker": worker, "state": "claimed"}, f)
    return True


def mark(job_key, state, **extra):
    p = _claim_path(job_key)
    rec = json.loads(p.read_text()) if p.exists() else {"job": job_key}
    rec.update(state=state, **extra)
    p.write_text(json.dumps(rec, ensure_ascii=False))


def claim_record(job_key):
    p = _claim_path(job_key)
    return json.loads(p.read_text()) if p.exists() else None


DECISIONS = {"sent": "사람이 직접 보냈거나 도착을 확인함", "skip": "사람이 보내지 않기로 함"}


def resolve(ex_id, ident, decision, note="", who=None):
    """사람이 확인 파일을 보고 내린 결정을 기록에 남긴다. 다음 실행은 이 항목을 다시 묻지도 보내지도 않는다.
    (BorkoB 질문: 손으로 다시 보낸 결정이 기록으로 돌아가는가?)"""
    if decision not in DECISIONS:
        raise ValueError(f"결정은 {', '.join(DECISIONS)} 중 하나여야 해요")
    review = REVIEW_DIR / f"{ex_id}.json"
    if not review.exists():
        raise FileNotFoundError(f"실행 {ex_id}의 확인 파일이 없어요 ({review}). 먼저 recover로 계획을 만드세요.")
    items = {str(x["id"]): x for x in json.loads(review.read_text())}
    if str(ident) not in items:
        raise KeyError(f"확인 파일에 {ident}가 없어요. 있는 항목: {', '.join(items)}")
    url = items[str(ident)]["first_attempt_request"]["url"]
    job = f"{ex_id}:{url}:{ident}"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    claim(job, worker="human")                  # 기록이 없으면 만든다 (이미 있으면 그대로 둠)
    mark(job, "resolved", resolution={
        "decision": decision, "meaning": DECISIONS[decision], "note": note,
        "by": who or os.environ.get("USER", "unknown"), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    return job


def claim_state(job_key):
    p = _claim_path(job_key)
    return json.loads(p.read_text()).get("state") if p.exists() else None


def send_items(todo, plan, ex_id, worker="cli"):
    """잡기 → 'sending' 기록 → 보내기 → 결과 기록. 실제로 보낸 건수를 정직하게 돌려준다."""
    result = {"sent": 0, "failed": 0, "skipped_claimed": 0, "lines": []}
    for x in todo:
        job = f"{ex_id}:{plan['url']}:{x['id']}"
        if not claim(job, worker):
            result["skipped_claimed"] += 1
            result["lines"].append(f"{x['id']}: 다른 작업자가 이미 잡았거나 처리 기록이 있음 ({claim_state(job)}) → 건너뜀")
            continue
        headers = {"Content-Type": "application/json", "Idempotency-Key": x["key"]}
        # 보내기 '전에' 실제로 보낼 요청을 그대로 남긴다 (멈췄을 때 사람이 볼 것)
        mark(job, "sending", request={"method": plan["method"], "url": plan["url"],
                                      "headers": headers, "body": x["body"]},
             sending_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        code, _ = http(plan["method"], plan["url"], body=x["body"], headers={"Idempotency-Key": x["key"]})
        if isinstance(code, int) and 200 <= code < 300:
            mark(job, "sent", code=code)
            result["sent"] += 1
            result["lines"].append(f"{x['id']}: 보냄")
        else:
            mark(job, "unknown", code=code)   # 응답이 실패여도 써졌을 수 있음 → 사람 확인
            result["failed"] += 1
            result["lines"].append(f"{x['id']}: 응답 {code} → 써졌는지 알 수 없음, 사람 확인 필요")
    return result


# ---------- 복구 계획 ----------
def make_plan(execution, workflow, min_age=0, ex_id=None):
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
    svc = find_service(url)
    if not svc:
        sys.exit(f"이 주소({url})는 services.json에 없어서 처리 여부를 확인하는 방법을 몰라요.")
    field = svc["id_field"]
    mode = svc.get("lookup_mode", LOOKUP_MODE)
    settle = svc.get("settle_seconds", SETTLE_SECONDS)

    downstream = [c["node"] for outs in workflow["connections"].get(failed_name, {}).get("main", []) for c in outs]
    age = max(seconds_since(execution.get("stoppedAt")), min_age)
    # 노드 전체가 실패한 경우 n8n은 항목별 실패 이유를 남기지 않아서 "unknown"으로 본다
    failure = "unknown"
    plan = []
    for i, it in enumerate(items):
        body = build_body(p.get("jsonBody"), it["json"], i, rd["runData"])
        ident = body.get(field)
        found = lookup(svc, ident)
        status = decide(failure, found, age, lookup_mode=mode, settle=settle,
                        keyed_first_attempt=bool(svc.get("idempotent_first_attempt")))
        # 이전 복구 시도의 기록이 남아 있으면(예: 'sending'에서 멈춤) 다시 보내지 않고 사람에게
        rec = claim_record(f"{ex_id or execution.get('id')}:{url}:{ident}")
        prior = rec.get("state") if rec else None
        if prior == "resolved":
            r = rec.get("resolution", {})
            status = "사람이 처리함"            # 사람의 결정이 조회 결과보다 우선: 다시 묻지도, 보내지도 않음
            note = f"{r.get('meaning')} ({r.get('by')}, {r.get('at')})" + (f": {r['note']}" if r.get("note") else "")
        elif prior and status != "이미 처리됨":
            status = "확인 불가"
            note = f"이전 복구 기록이 '{prior}' 상태로 남아 있음"
        elif status == "확인 불가" and found is None:
            note = "조회가 true/false로 답하지 않음 (오류·이상한 응답)"
        elif status == "확인 불가":
            note = f"조회상 없음, 하지만 늦게 반영되는 서비스라 {settle}초가 지나도 '안 써졌다'는 증거가 안 됨"
        else:
            note = None
        plan.append({"index": i, "id": ident, "body": body, "status": status, "note": note, "lookup": found,
                     "key": f"{parsed.path.strip('/')}-{ident}"})
    return {"node": failed_name, "url": url, "method": p.get("method", "GET"), "field": field,
            "service": svc, "settle": settle,
            "error": (rd.get("error") or {}).get("message"), "plan": plan, "downstream": downstream}


REVIEW_DIR = HERE / ".ar_state" / "review"


def write_review(plan, ex_id):
    """사람에게 넘기는 항목마다, 판단에 필요한 걸 한 파일에 남긴다.

    주의: n8n은 실패한 노드가 실제로 보낸 바이트를 저장하지 않는다. 그래서 첫 시도의 요청은
    n8n 실행 기록(앞 노드 출력 + 노드 설정)으로 '다시 만든 것'이고, 그 사실을 파일에 적는다.
    복구 도구가 보낸 요청은 보내기 전에 claims 기록에 그대로 남긴다.
    """
    items = [x for x in plan["plan"] if x["status"] == "확인 불가"]
    if not items:
        return None
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for x in items:
        job = f"{ex_id}:{plan['url']}:{x['id']}"
        p = _claim_path(job)
        out.append({
            "id_field": plan["field"], "id": x["id"],
            "why_not_sent": x.get("note"),
            "lookup_answer": x.get("lookup"),
            "n8n_error": plan["error"],
            "first_attempt_request": {
                "method": plan["method"], "url": plan["url"], "body": x["body"],
                "source": "n8n이 저장하지 않아서 실행 기록으로 다시 만든 것 (보낸 바이트 그대로가 아님)",
            },
            "recovery_attempt": json.loads(p.read_text()) if p.exists() else None,
            "to_check": f"상대 서비스에서 {plan['field']}={x['id']}가 실제로 있는지 직접 확인",
        })
    path = REVIEW_DIR / f"{ex_id}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return path


def show(plan):
    done = [x for x in plan["plan"] if x["status"] == "이미 처리됨"]
    todo = [x for x in plan["plan"] if x["status"] == "다시 보내야 함"]
    unknown = [x for x in plan["plan"] if x["status"] == "확인 불가"]
    waiting = [x for x in plan["plan"] if x["status"] == "확인 대기"]
    human = [x for x in plan["plan"] if x["status"] == "사람이 처리함"]
    print(f"\n실패한 노드: {plan['node']}  ({plan['method']} {plan['url']})")
    print(f"n8n 에러: {plan['error']}")
    print(f"항목 {len(plan['plan'])}개를 하나씩 조회한 결과:")
    print(f"  이미 처리됨     {len(done):>3}개  → 다시 보내지 않음")
    print(f"  다시 보내야 함  {len(todo):>3}개  → 보내기 전에 기록을 남기고 중복 방지 키를 붙여서 보냄")
    if unknown:
        print(f"  확인 불가       {len(unknown):>3}개  → 써졌는지 확신할 수 없어 보내지 않음 (사람이 확인 필요)")
    for x in unknown:
        print(f"    - {plan['field']}={x['id']}: {x.get('note')}")
    if human:
        print(f"  사람이 처리함   {len(human):>3}개  → 기록된 사람의 결정대로 둠 (다시 묻지 않음)")
    for x in human:
        print(f"    - {plan['field']}={x['id']}: {x.get('note')}")
    if waiting:
        print(f"  확인 대기       {len(waiting):>3}개  → 기록이 늦게 반영될 수 있어 {plan['settle']}초 뒤 다시 조회해야 함")
    for x in todo[:10]:
        print(f"    - {plan['field']}={x['id']}")
    print(f"\n참고: n8n 기본 Retry였다면 {len(plan['plan'])}건을 전부 다시 보냈을 거예요.")
    if plan["downstream"]:
        print(f"주의: 이 노드 다음에 {plan['downstream']} 노드가 있어요. v0.1은 거기까지 이어서 실행하지 않아요.")
    return todo


def run(todo, plan, ex_id):
    r = send_items(todo, plan, ex_id)
    for line in r["lines"]:
        print(f"  {line}")
    print(f"\n보낸 요청: {r['sent']}건 성공, {r['failed']}건 응답 실패, {r['skipped_claimed']}건 건너뜀 (이미 잡힌 항목)")
    print("실제로 중복이 없는지는 상대 서비스의 기록으로 확인하세요. 이 도구는 보낸 횟수만 셀 수 있어요.")


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
    plan = make_plan(execution, workflow, ex_id=ex_id)
    todo = show(plan)
    review = write_review(plan, ex_id)
    if review:
        print(f"사람 확인용 기록: {review}")
        print(f"확인한 뒤 결정을 남기세요: python3 ar.py resolve {ex_id} <항목값> --sent (보냈음/도착 확인) 또는 --skip (안 보냄)")
    if not todo:
        rest = [x for x in plan["plan"] if x["status"] != "이미 처리됨"]
        if rest:
            print(f"\n지금 자동으로 다시 보낼 건 없어요. {len(rest)}건은 확인 대기 또는 사람 확인이 필요해요.")
        else:
            print("\n다시 보낼 게 없어요. 모든 항목이 이미 처리된 걸로 확인됐어요.")
        return
    if "--yes" not in args:
        if input("\n위 계획대로 보낼까요? (y/n) ").strip().lower() != "y":
            print("취소했어요. 아무것도 보내지 않았어요.")
            return
    run(todo, plan, ex_id)


if __name__ == "__main__":
    main()
