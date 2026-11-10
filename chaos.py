"""Agent Recovery - 실패 주입 테스트 (chaos test)

배포 전에 "이 워크플로가 실패하면 같은 일을 두 번 하는가?"를 시험해요.
아이디어 출처: n8n 커뮤니티 ccch713 - "일부러 실패를 넣는 테스트를 워크플로를 바꿀 때마다 돌린다"
https://community.n8n.io/t/327579

  python3 ar.py chaos prepare <워크플로ID>     시험용 복사본(JSON) 만들기
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

판정 기준
  중복: 중복 방지 키(Idempotency-Key) 없이 같은 요청이 두 번 이상 기록됨 → 실패
  누락: 한 번이라도 시도됐는데 끝내 기록되지 않음 → 실패
  안전한 재시도: 같은 Idempotency-Key로 다시 온 요청 → 기록하지 않고 '중복 방지됨'으로 셈 (진짜 서비스가
               키를 지원할 때의 동작을 흉내 냄)

한계
  - 키가 없으면 '같은 요청'을 본문 내용으로 판단해요. 원래 같은 내용을 두 번 보내는 게 맞는 워크플로라면
    중복으로 잘못 셀 수 있어요.
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


# ---------------- 시험용 가짜 목적지 ----------------
class Sink:
    def __init__(self, fail_nth=33, late_nth=0, late_seconds=10):
        self.cfg = {"fail_nth": fail_nth, "late_nth": late_nth, "late_seconds": late_seconds}
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.n = 0                    # 쓰기 요청 순번
            self.seen = {}                # 요청 식별값 → 첫 시도 정보
            self.recorded = {}            # 요청 식별값 → 기록된 횟수
            self.deduped = 0              # 같은 키로 다시 와서 무시한 횟수
            self.injected_503 = 0
            self.late = 0
            self.log = []

    def handle(self, method, path, headers, body):
        key_header = headers.get("Idempotency-Key")
        ident = f"key:{key_header}" if key_header else \
            "body:" + hashlib.sha1(f"{method} {path} {body}".encode()).hexdigest()[:16]
        with self.lock:
            self.n += 1
            n = self.n
            self.seen.setdefault(ident, {"path": path, "body": body[:200], "keyed": bool(key_header)})
            if self.cfg["fail_nth"] and n == self.cfg["fail_nth"]:
                self.injected_503 += 1
                self.log.append((n, ident, "503 주입 (기록 안 됨)"))
                return 503, 0
            if key_header and self.recorded.get(ident):
                self.deduped += 1
                self.log.append((n, ident, "같은 키 → 중복 방지됨"))
                return 200, 0
            self.recorded[ident] = self.recorded.get(ident, 0) + 1
            delay = self.cfg["late_seconds"] if self.cfg["late_nth"] and n == self.cfg["late_nth"] else 0
            if delay:
                self.late += 1
            self.log.append((n, ident, f"기록됨{' (응답은 ' + str(delay) + '초 뒤)' if delay else ''}"))
            return 200, delay

    def report(self):
        with self.lock:
            dups = {k: c for k, c in self.recorded.items() if c > 1}
            missing = [k for k in self.seen if not self.recorded.get(k)]
            problems = []
            if dups:
                problems.append(f"중복 {sum(c - 1 for c in dups.values())}건: 같은 요청이 키 없이 두 번 이상 기록됨 ({len(dups)}개 항목)")
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
                "중복 예시": [self.seen[k]["body"] for k in list(dups)[:3]],
                "누락 예시": [self.seen[k]["body"] for k in missing[:3]],
                "키를 붙인 항목 비율": f"{sum(1 for v in self.seen.values() if v['keyed'])}/{len(self.seen)}",
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
            self._send(code, {"ok": code == 200} if code == 200 else {"error": "Service Unavailable (chaos test)"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _write
    return H


def serve(fail_nth, late_nth, late_seconds, port=PORT):
    sink = Sink(fail_nth, late_nth, late_seconds)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(sink))
    return srv, sink


def print_report(r):
    print(f"\n판정: {r['판정']}")
    for p in r["문제"]:
        print(f"  ✗ {p}")
    print(f"\n쓰기 요청 {r['쓰기 요청 수']}번 / 서로 다른 항목 {r['서로 다른 항목']}개 / 기록된 항목 {r['기록된 항목']}개")
    print(f"주입한 503 {r['주입한 503']}번, 늦은 응답 {r['늦은 응답']}번, 중복 방지된 재시도 {r['중복 방지된 재시도']}번")
    print(f"중복 방지 키를 붙인 항목: {r['키를 붙인 항목 비율']}")
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
    sub.add_parser("report")
    sub.add_parser("reset")
    p3 = sub.add_parser("run")
    p3.add_argument("webhook_url")
    p3.add_argument("--wait", type=int, default=120, help="워크플로가 끝나길 기다릴 최대 초")
    p3.add_argument("--settle", type=int, default=3, help="끝난 뒤 늦은 응답을 기다릴 초")
    a = ap.parse_args(argv)

    if a.cmd == "prepare":
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
    elif a.cmd == "serve":
        srv, _ = serve(a.fail_nth, a.late_nth, a.late_seconds)
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
