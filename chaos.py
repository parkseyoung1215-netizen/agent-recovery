"""Agent Recovery - 실패 주입 테스트 (chaos test)

배포 전에 "이 워크플로가 실패하면 같은 일을 두 번 하는가?"를 시험해요.
아이디어 출처: n8n 커뮤니티 ccch713 - "일부러 실패를 넣는 테스트를 워크플로를 바꿀 때마다 돌린다"
https://community.n8n.io/t/327579

  python3 ar.py chaos prepare <워크플로ID 또는 내보낸.json>   시험용 복사본(JSON) 만들기 (파일이면 API 키 필요 없음)
      → HTTP 쓰기 요청이 진짜 서비스 대신 시험용 가짜 목적지(127.0.0.1:8200)로 가도록 바꾼 복사본.
        n8n에 붙여넣어서 실행하면 돼요. 시험 중에는 진짜 메일·결제가 절대 나가지 않아요.
  python3 ar.py chaos serve [옵션]              시험용 가짜 목적지 켜기
      --fail-nth 33        33번째 쓰기 요청은 503으로 거절 (기록 안 됨)
      --late-nth 0         N번째 쓰기 요청은 기록한 뒤 늦게 응답 (0이면 안 함)
      --late-seconds 10    늦게 응답할 때 몇 초 뒤에
  python3 ar.py chaos report                    결과 보기 (통과/실패)
  python3 ar.py chaos run <웹훅주소>               한 줄로 시험: 기록 지우기 → 워크플로 실행 → 판정 (CI용)
      → prepare가 시험용 복사본에 'chaos test trigger' 웹훅을 넣어둬요. n8n에서 그 복사본을 게시(Publish)하면
        웹훅 주소(예: http://127.0.0.1:5678/webhook/chaos-<워크플로ID>)로 실행할 수 있어요.
        통과면 종료 코드 0, 실패면 1이라 CI에서 그대로 쓸 수 있어요.
  python3 ar.py chaos reset                     기록 지우기 (같은 실패 설정으로 다시 시험)
  python3 ar.py chaos learn <워크플로ID>          실제로 성공한 실행에서 '계약' 배우기
      → 진짜 API가 성공으로 받아준 요청의 필드·타입과, 진짜 응답의 예시를 contracts.json에 저장.
        목적지(메서드+주소)마다 하나라서, 여러 워크플로에서 배운 게 한 파일에 합쳐져요 (kartike 제안).
        serve --contract contracts.json 으로 켜면 가짜 목적지가 그 계약에 없는 모양의 요청을
        400으로 거절하고(판정 실패), 응답도 진짜 응답 예시를 돌려줘요.
        아이디어 출처: n8n 커뮤니티 EKH1 - "가짜 목적지가 실제 API와 점점 달라지는 게 진짜 문제"

판정 기준
  중복: 같은 요청(메서드+경로+본문)이 두 번 이상 기록됨 → 실패. 키가 없을 때뿐 아니라
        키가 시도마다 바뀔 때도 해당 (같은 키로 다시 온 요청만 기록하지 않음)
  누락: 한 번이라도 시도됐는데 끝내 기록되지 않음 → 실패
  키 공유: 서로 다른 요청이 같은 Idempotency-Key를 씀 (예: 실행 ID를 키로 씀) → 실패
  미시도: --expect N 을 주면, N건 중 아예 시도되지 않은 항목도 실패로 셈
  (--id-field to 를 주면 본문 전체 대신 그 필드로 항목을 구분: 본문에 시각처럼 매번 바뀌는 값이 있을 때)
  (세 가지 모두 feversoul이 찾아준 '통과하면 안 되는데 통과하던' 경우)
  안전한 재시도: 같은 Idempotency-Key로 다시 온 요청 → 기록하지 않고 '중복 방지됨'으로 셈 (진짜 서비스가
               키를 지원할 때의 동작을 흉내 냄)

한계
  - 키가 없으면 '같은 요청'을 본문 내용으로 판단해요. 원래 같은 내용을 두 번 보내는 게 맞는 워크플로라면
    중복으로 잘못 셀 수 있어요.
  - 계약은 '진짜 API가 받아준 적 있는 모양'일 뿐, 진짜 API의 규칙 전체가 아니에요. 성공한 실행에서 본 적 없는
    필드는 진짜로 괜찮아도 위반으로 잡고, 진짜 API가 규칙을 바꾸면 다시 learn 하기 전까지는 모르는 채로 통과해요.
  - 읽기(GET) 요청은 빈 응답 {}을 돌려줘요. 응답 내용에 따라 다음 단계가 달라지는 워크플로는 실제와 다를 수 있어요.
  - URL이 식(={{ ... }})으로 된 노드는 자동으로 바꾸지 못하고 알려주기만 해요.
"""
import argparse
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PORT = 8200
SINK = f"http://127.0.0.1:{PORT}/sink/"
WRITE = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------- 준비: 시험용 복사본 만들기 ----------------
def prepare(workflow):
    """HTTP 쓰기 노드의 URL을 시험용 가짜 목적지로 바꾼 복사본과, 바꾸지 못한 노드 목록을 돌려준다."""
    wf = json.loads(json.dumps(workflow))
    changed, skipped = [], []
    for node in wf.get("nodes", []):
        if node.get("type") != "n8n-nodes-base.httpRequest":
            continue
        p = node.get("parameters", {})
        method = (p.get("method") or "GET").upper()
        url = p.get("url", "")
        if method not in WRITE:
            continue
        if url.startswith("="):
            skipped.append(f"{node['name']}: URL이 식이라 자동으로 바꾸지 못함 ({url[:60]})")
            continue
        u = urllib.parse.urlparse(url)
        p["url"] = f"{SINK}{u.scheme}/{u.netloc}{u.path}" + (f"?{u.query}" if u.query else "")
        changed.append(f"{node['name']}: {method} {url} → {p['url']}")
    # 원래 트리거가 웹훅이면 복사본에서는 주소를 바꾼다. 같은 주소면 원본이 켜져 있는 동안 n8n이 복사본을 켜지 못함
    for node in wf.get("nodes", []):
        if node.get("type") == "n8n-nodes-base.webhook" and node.get("name") != "chaos test trigger":
            p = node.setdefault("parameters", {})
            p["path"] = f"{p.get('path') or node['name']}-chaos-copy"
            node.pop("webhookId", None)
            changed.append(f"{node['name']}: 원래 웹훅 주소를 '{p['path']}'로 바꿈 (원본과 겹치지 않게)")
    add_test_trigger(wf)
    keep = {"name", "nodes", "connections", "settings"}
    wf = {k: v for k, v in wf.items() if k in keep}
    wf["name"] = "[chaos test] " + wf.get("name", "workflow")
    return wf, changed, skipped


TRIGGER_TYPES = ("Trigger", "manualTrigger", "webhook")


def add_test_trigger(wf):
    """시험용 웹훅 트리거를 넣어, 원래 트리거가 하던 것처럼 다음 노드를 실행하게 한다."""
    nodes = wf.setdefault("nodes", [])
    conns = wf.setdefault("connections", {})
    if any(n["name"] == "chaos test trigger" for n in nodes):
        return
    trigger = next((n for n in nodes if n.get("type", "").endswith(TRIGGER_TYPES)), None)
    targets = conns.get(trigger["name"], {}).get("main", [[]]) if trigger else [[]]
    wid = str(wf.get("id") or "workflow")
    nodes.append({
        "parameters": {"httpMethod": "POST", "path": f"chaos-{wid}", "responseMode": "lastNode", "options": {}},
        "name": "chaos test trigger", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300], "webhookId": f"chaos-{wid}",
    })
    conns["chaos test trigger"] = {"main": json.loads(json.dumps(targets))}


# ---------------- 계약: 진짜 API가 받아준 요청의 모양 ----------------
def _jtype(v):
    return {bool: "boolean", int: "number", float: "number", str: "string",
            list: "array", dict: "object", type(None): "null"}.get(type(v), "other")


def learn(workflow, executions):
    """성공한 실행들에서, HTTP 쓰기 노드마다 진짜 API가 받아준 요청의 필드·타입과 응답 예시를 모은다."""
    import recover
    contracts = {}
    for node in workflow.get("nodes", []):
        p = node.get("parameters", {})
        method = (p.get("method") or "GET").upper()
        url = p.get("url", "")
        if node.get("type") != "n8n-nodes-base.httpRequest" or method not in WRITE or url.startswith("="):
            continue
        u = urllib.parse.urlparse(url)
        key = f"{method} {u.scheme}/{u.netloc}{u.path}"
        c = {"node": node["name"], "requests": 0, "fields": {}, "response_example": None, "executions": []}
        for ex in executions:
            runs = ex["data"]["resultData"].get("runData", {})
            run = (runs.get(node["name"]) or [None])[-1]
            if not run or run.get("error"):
                continue
            parent = ((run.get("source") or [{}])[0] or {}).get("previousNode")
            items = recover.output_items(runs, parent) if parent else []
            outs = recover.output_items(runs, node["name"])
            for i, it in enumerate(items):
                try:
                    body = recover.build_body(p.get("jsonBody"), it["json"], i, runs)
                except ValueError:
                    continue
                c["requests"] += 1
                for f, v in body.items():
                    e = c["fields"].setdefault(f, {"types": [], "seen": 0})
                    e["seen"] += 1
                    if _jtype(v) not in e["types"]:
                        e["types"].append(_jtype(v))
            if outs and c["response_example"] is None:
                c["response_example"] = outs[0].get("json")
            c["executions"].append(str(ex.get("id")))
        if c["requests"]:
            contracts[key] = c
    return {"learned_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "contracts": contracts}


def merge(shared, new):
    """API마다 한 번만 기록: 같은 목적지(메서드+주소)는 여러 워크플로에서 배운 걸 합친다. (kartike 제안)"""
    out = json.loads(json.dumps(shared or {"contracts": {}}))
    out.setdefault("contracts", {})
    for key, c in new.get("contracts", {}).items():
        old = out["contracts"].get(key)
        if not old:
            out["contracts"][key] = c
            continue
        for f, e in c["fields"].items():
            o = old["fields"].setdefault(f, {"types": [], "seen": 0})
            o["seen"] += e["seen"]
            o["types"] += [t for t in e["types"] if t not in o["types"]]
        old["requests"] += c["requests"]
        old["executions"] = sorted(set(old.get("executions", [])) | set(c.get("executions", [])))
        old["node"] = ", ".join(sorted(set(old["node"].split(", ")) | {c["node"]}))
        if old.get("response_example") is None:
            old["response_example"] = c.get("response_example")
    out["learned_at"] = new.get("learned_at")
    return out


def check_contract(contract, body_text):
    """요청 본문이 계약과 다르면 이유 목록을 돌려준다 (같으면 빈 목록)."""
    try:
        body = json.loads(body_text) if body_text else {}
    except json.JSONDecodeError:
        return ["본문이 JSON이 아님"]
    if not isinstance(body, dict):
        return ["본문이 JSON 객체가 아님"]
    fields, why = contract["fields"], []
    for f, e in fields.items():
        if e["seen"] == contract["requests"] and f not in body:
            why.append(f"필수 필드 '{f}'가 없음")
    for f, v in body.items():
        if f not in fields:
            why.append(f"성공한 실행에서 본 적 없는 필드 '{f}'")
        elif _jtype(v) not in fields[f]["types"]:
            why.append(f"'{f}'의 타입이 {_jtype(v)} (진짜 API가 받아준 건 {'/'.join(fields[f]['types'])})")
    return why


# ---------------- 시험용 가짜 목적지 ----------------
class Sink:
    def __init__(self, fail_nth=33, late_nth=0, late_seconds=10, contract=None, id_field=None, expect=0):
        self.cfg = {"fail_nth": fail_nth, "late_nth": late_nth, "late_seconds": late_seconds}
        self.id_field = id_field          # 항목을 본문 전체 대신 이 필드로 구분 (본문에 시각 등이 섞여 있을 때)
        self.expect = expect              # 보내져야 할 항목 수. 아예 시도도 안 된 항목을 누락으로 잡기 위해
        self.contract = contract or {}
        self.lock = threading.Lock()
        self.reset()

    def _item_of(self, body):
        """중복을 셀 때 쓰는 항목 식별값: --id-field가 있으면 그 필드 값, 없으면 본문 전체"""
        if self.id_field:
            try:
                v = json.loads(body).get(self.id_field)
                if v is not None:
                    return f"{self.id_field}={v}"
            except (json.JSONDecodeError, AttributeError):
                pass
        return body

    def _contract_for(self, method, path):
        rest = path.split("?", 1)[0]
        rest = rest[len("/sink/"):] if rest.startswith("/sink/") else rest.lstrip("/")
        return (self.contract.get("contracts") or {}).get(f"{method} {rest}")

    def response_for(self, method, path):
        c = self._contract_for(method, path)
        return c.get("response_example") if c and c.get("response_example") is not None else {"ok": True}

    def reset(self):
        with self.lock:
            self.n = 0                    # 쓰기 요청 순번
            self.seen = {}                # 요청 식별값 → 첫 시도 정보
            self.recorded = {}            # 요청 식별값 → 기록된 횟수
            self.written = {}             # 요청 내용(메서드+경로+본문) → 기록된 횟수
            self.key_bodies = {}          # 키 → 그 키로 온 서로 다른 내용들 (키 하나를 여러 항목이 같이 쓰는지)
            self.deduped = 0              # 같은 키로 다시 와서 무시한 횟수
            self.injected_503 = 0
            self.late = 0
            self.violations = []          # 계약 위반 (진짜 API라면 거절했을 수 있는 요청)
            self.log = []

    def handle(self, method, path, headers, body):
        key_header = headers.get("Idempotency-Key")
        content = "body:" + hashlib.sha1(f"{method} {path} {self._item_of(body)}".encode()).hexdigest()[:16]
        ident = f"key:{key_header}" if key_header else content
        with self.lock:
            self.n += 1
            n = self.n
            self.seen.setdefault(ident, {"path": path, "body": body[:200], "keyed": bool(key_header),
                                         "content": content})
            if key_header:
                self.key_bodies.setdefault(key_header, {}).setdefault(content, body[:200])
            c = self._contract_for(method, path)
            why = check_contract(c, body) if c else []
            if why:
                self.violations.append({"body": body[:200], "why": why})
                self.log.append((n, ident, "계약 위반 → 400"))
                return 400, 0
            if self.cfg["fail_nth"] and n == self.cfg["fail_nth"]:
                self.injected_503 += 1
                self.log.append((n, ident, "503 주입 (기록 안 됨)"))
                return 503, 0
            if key_header and self.recorded.get(ident):
                self.deduped += 1
                self.log.append((n, ident, "같은 키 → 중복 방지됨"))
                return 200, 0
            self.recorded[ident] = self.recorded.get(ident, 0) + 1
            self.written[content] = self.written.get(content, 0) + 1
            delay = self.cfg["late_seconds"] if self.cfg["late_nth"] and n == self.cfg["late_nth"] else 0
            if delay:
                self.late += 1
            self.log.append((n, ident, f"기록됨{' (응답은 ' + str(delay) + '초 뒤)' if delay else ''}"))
            return 200, delay

    def report(self):
        with self.lock:
            # 중복은 내용으로 센다: 시도마다 키가 바뀌면(예: $randomUUID) 키로는 서로 다른 요청처럼 보이기 때문
            dups = {c: n for c, n in self.written.items() if n > 1}
            example = {v["content"]: v["body"] for v in self.seen.values()}
            missing = [k for k, v in self.seen.items()
                       if not self.recorded.get(k) and not self.written.get(v["content"])]
            problems = []
            if dups:
                problems.append(f"중복 {sum(n - 1 for n in dups.values())}건: 같은 요청이 두 번 이상 기록됨 ({len(dups)}개 항목)."
                                " 키가 없거나, 키가 시도마다 바뀜")
            shared = {k: v for k, v in self.key_bodies.items() if len(v) > 1}
            if shared:
                problems.append(f"키 공유 {sum(len(v) - 1 for v in shared.values())}건: 서로 다른 요청이 같은 Idempotency-Key를 씀"
                                f" ({len(shared)}개 키). 진짜 서비스는 뒤에 온 것을 중복으로 보고 버리거나 거절해요")
            distinct = len({v["content"] for v in self.seen.values()})
            never = max(self.expect - distinct, 0) if self.expect else 0
            if never:
                problems.append(f"미시도 {never}건: {self.expect}건이 보내져야 하는데 {distinct}건만 시도됨 (워크플로가 중간에 멈춤)")
            if self.violations:
                problems.append(f"계약 위반 {len(self.violations)}건: 진짜 API가 성공으로 받아준 적 없는 모양의 요청")
            if missing:
                problems.append(f"누락 {len(missing)}건: 시도는 됐는데 끝내 기록되지 않음")
            return {
                "판정": "통과" if not problems else "실패",
                "문제": problems,
                "실패 설정": self.cfg,
                "쓰기 요청 수": self.n,
                "서로 다른 항목": len(self.seen),
                "기록된 항목": sum(1 for c in self.recorded.values() if c),
                "주입한 503": self.injected_503,
                "늦은 응답": self.late,
                "중복 방지된 재시도": self.deduped,
                "중복 예시": [example[c] for c in list(dups)[:3]],
                "누락 예시": [self.seen[k]["body"] for k in missing[:3]],
                "키를 붙인 항목 비율": f"{sum(1 for v in self.seen.values() if v['keyed'])}/{len(self.seen)}",
                "계약": (f"{self.contract.get('learned_at')}에 배운 것, {len(self.contract.get('contracts', {}))}개 목적지"
                         if self.contract else "없음 (모든 요청을 받아줌)"),
                "계약 위반 예시": self.violations[:3],
            }


def make_handler(sink):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path.startswith("/report"):
                return self._send(200, sink.report())
            if self.path.startswith("/reset"):
                sink.reset()
                return self._send(200, {"ok": True, "message": "기록을 지웠어요"})
            return self._send(200, {})            # 시험 중 읽기 요청에는 빈 응답

        def _write(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            code, delay = sink.handle(self.command, self.path, self.headers, body)
            if delay:
                time.sleep(delay)
            if code == 200:
                self._send(200, sink.response_for(self.command, self.path))
            elif code == 400:
                self._send(400, {"error": "Bad Request (chaos test: 계약 위반)"})
            else:
                self._send(code, {"error": "Service Unavailable (chaos test)"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _write
    return H


class Server(ThreadingHTTPServer):
    # 기본값 5면 n8n이 50건을 동시에 보낼 때 대기열이 넘쳐서 운영체제가 연결을 끊어요(ECONNRESET).
    # 그러면 주입하지 않은 실패가 생기고, 끊긴 요청은 기록에도 안 남아서 판정이 틀려져요.
    request_queue_size = 128
    daemon_threads = True


def serve(fail_nth, late_nth, late_seconds, port=PORT, contract=None, id_field=None, expect=0):
    sink = Sink(fail_nth, late_nth, late_seconds, contract, id_field, expect)
    srv = Server(("127.0.0.1", port), make_handler(sink))
    return srv, sink


def print_report(r):
    print(f"\n판정: {r['판정']}")
    for p in r["문제"]:
        print(f"  ✗ {p}")
    print(f"\n쓰기 요청 {r['쓰기 요청 수']}번 / 서로 다른 항목 {r['서로 다른 항목']}개 / 기록된 항목 {r['기록된 항목']}개")
    print(f"주입한 503 {r['주입한 503']}번, 늦은 응답 {r['늦은 응답']}번, 중복 방지된 재시도 {r['중복 방지된 재시도']}번")
    print(f"중복 방지 키를 붙인 항목: {r['키를 붙인 항목 비율']}")
    print(f"계약: {r.get('계약', '없음')}")
    for v in r.get("계약 위반 예시") or []:
        print(f"  계약 위반: {v['body']}\n    → {'; '.join(v['why'])}")
    if r["중복 예시"]:
        print("중복 예시:", *r["중복 예시"], sep="\n  ")
    if r["누락 예시"]:
        print("누락 예시:", *r["누락 예시"], sep="\n  ")
    if r["판정"] == "실패":
        print("\n고치는 방법 예: 쓰기 요청에 항목마다 고정된 Idempotency-Key 헤더를 붙이고(상대 서비스가 지원할 때),"
              "\n자동 재시도는 키가 있을 때만 켜세요. 키를 지원하지 않는 서비스면 ar.py check / recover로 사람이 확인하게 하세요.")


def _sink(path, timeout=5):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/{path}", timeout=timeout) as r:
        return json.loads(r.read())


def run(webhook_url, wait=120, settle=3):
    """기록 지우기 → 웹훅으로 워크플로 실행 → 판정. 통과면 0, 실패면 1, 시험 자체가 안 되면 2."""
    try:
        _sink("reset")
    except urllib.error.URLError:
        print("시험용 가짜 목적지가 꺼져 있어요. 먼저 python3 ar.py chaos serve 를 켜세요.")
        return 2
    print(f"워크플로 실행: {webhook_url}")
    req = urllib.request.Request(webhook_url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=wait) as r:
            print(f"  워크플로 응답: {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  워크플로 응답: {e.code} (워크플로가 실패로 끝났을 수 있음 - 판정은 기록으로 함)")
        if e.code == 404:
            print("  웹훅을 찾지 못했어요. n8n에서 시험용 복사본을 게시(Publish)했는지 확인하세요.")
            return 2
    except urllib.error.URLError as e:
        print(f"  워크플로를 실행하지 못했어요: {e.reason}")
        return 2
    except TimeoutError:                  # 응답을 기다리다 끊긴 경우는 URLError로 감싸지지 않음
        print(f"  워크플로가 {wait}초 안에 끝나지 않았어요. --wait 을 늘려서 다시 시험하세요.")
        return 2
    time.sleep(settle)
    r = _sink("report")
    if r["쓰기 요청 수"] == 0:
        print("  쓰기 요청이 하나도 오지 않았어요. 시험용 복사본(prepare 결과)을 실행한 게 맞는지 확인하세요.")
        return 2
    print_report(r)
    return 0 if r["판정"] == "통과" else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ar.py chaos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("prepare")
    p1.add_argument("workflow_id")
    p1.add_argument("--out")
    p2 = sub.add_parser("serve")
    p2.add_argument("--fail-nth", type=int, default=33)
    p2.add_argument("--late-nth", type=int, default=0)
    p2.add_argument("--late-seconds", type=int, default=10)
    p2.add_argument("--contract", help="chaos learn으로 만든 계약 파일")
    p2.add_argument("--id-field", help="항목을 이 본문 필드로 구분 (예: to). 본문에 시각처럼 매번 바뀌는 값이 있을 때")
    p2.add_argument("--expect", type=int, default=0, help="보내져야 할 항목 수. 시도조차 안 된 항목도 누락으로 잡음")
    p4 = sub.add_parser("learn")
    p4.add_argument("workflow_id")
    p4.add_argument("--limit", type=int, default=20, help="최근 성공한 실행 몇 개에서 배울지")
    p4.add_argument("--out")
    sub.add_parser("report")
    sub.add_parser("reset")
    p3 = sub.add_parser("run")
    p3.add_argument("webhook_url")
    p3.add_argument("--wait", type=int, default=120, help="워크플로가 끝나길 기다릴 최대 초")
    p3.add_argument("--settle", type=int, default=3, help="끝난 뒤 늦은 응답을 기다릴 초")
    a = ap.parse_args(argv)

    if a.cmd == "prepare":
        src = Path(a.workflow_id)
        if src.suffix == ".json" and src.exists():      # n8n에서 내보낸 파일이면 API 키 없이
            wf = json.loads(src.read_text(encoding="utf-8"))
            wf.setdefault("id", src.stem)
            a.workflow_id = str(wf["id"])
        else:
            import recover
            wf = recover.n8n(f"/workflows/{a.workflow_id}", recover.load_api_key())
        copy, changed, skipped = prepare(wf)
        out = Path(a.out or f"chaos_{a.workflow_id}.json")
        out.write_text(json.dumps(copy, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"시험용 복사본: {out}")
        for c in changed:
            print(f"  바꿈: {c}")
        for s in skipped:
            print(f"  주의: {s}")
        if not changed:
            print("  바꿀 HTTP 쓰기 노드가 없어요.")
        print(f"\n다음: 1) python3 ar.py chaos serve  2) 이 JSON을 n8n에 붙여넣고 게시(Publish)"
              f"\n      3) python3 ar.py chaos run http://127.0.0.1:5678/webhook/chaos-{a.workflow_id}")
    elif a.cmd == "learn":
        import recover
        key = recover.load_api_key()
        wf = recover.n8n(f"/workflows/{a.workflow_id}", key)
        lst = recover.n8n(f"/executions?workflowId={a.workflow_id}&status=success&limit={a.limit}", key)
        exs = [recover.n8n(f"/executions/{e['id']}?includeData=true", key) for e in lst.get("data", [])]
        new = learn(wf, exs)
        out = Path(a.out or "contracts.json")
        shared = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        c = merge(shared, new)
        out.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"성공한 실행 {len(exs)}개에서 배워서 {out}에 합쳤어요 (목적지 {len(c['contracts'])}개)")
        for k, v in c["contracts"].items():
            req = [f for f, e in v["fields"].items() if e["seen"] == v["requests"]]
            print(f"  {k}  (요청 {v['requests']}건, 필수 필드: {', '.join(req) or '없음'})")
        if not c["contracts"]:
            print("  배울 게 없어요. 진짜 서비스로 성공한 실행이 있어야 해요.")
    elif a.cmd == "serve":
        contract = json.loads(Path(a.contract).read_text(encoding="utf-8")) if a.contract else None
        srv, _ = serve(a.fail_nth, a.late_nth, a.late_seconds, contract=contract, id_field=a.id_field, expect=a.expect)
        if contract:
            print(f"계약 사용: {a.contract} ({contract.get('learned_at')}에 배움)")
        print(f"시험용 가짜 목적지 실행 중: {SINK}  (끄려면 Ctrl + C)")
        print(f"실패 설정: {a.fail_nth}번째 쓰기 요청 503" +
              (f", {a.late_nth}번째는 {a.late_seconds}초 늦게 응답" if a.late_nth else ""))
        srv.serve_forever()
    elif a.cmd == "run":
        sys.exit(run(a.webhook_url, a.wait, a.settle))
    elif a.cmd in ("report", "reset"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/{a.cmd}", timeout=5) as r:
                data = json.loads(r.read())
        except urllib.error.URLError:
            sys.exit("시험용 가짜 목적지가 꺼져 있어요. 먼저 python3 ar.py chaos serve 를 켜세요.")
        print_report(data) if a.cmd == "report" else print(data["message"])


if __name__ == "__main__":
    main()
