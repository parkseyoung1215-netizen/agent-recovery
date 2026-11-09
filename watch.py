"""Agent Recovery 감시기 v0.1 - n8n 실패를 자동으로 찾아서 복구 계획 만들기

켜두면 10초마다 n8n에 새로 실패한 실행이 있는지 확인해요.
실패를 찾으면 recover.py와 같은 방법으로 복구 계획을 만들고, 맥 알림을 띄워요.
세영 님이 승인 페이지에서 [승인]을 누를 때만 실제로 보내요.

실행:      python3 watch.py
승인 페이지: 크롬에서 http://127.0.0.1:8100
끄기:      Ctrl + C

- n8n은 읽기만 해요 (읽기 전용 키로 충분).
- 감시기를 켜기 전에 이미 있던 실패는 건드리지 않아요.
- 승인 후 보내기 직전에 항목마다 "이미 처리됐는지" 한 번 더 확인해요.
"""
import html
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import recover

PORT = 8100
POLL_SECONDS = int(__import__("os").environ.get("POLL", "10"))

lock = threading.Lock()
plans = {}          # 실행ID → 계획
seen = set()        # 이미 본 실패 실행ID
first_seen = {}     # 실행ID → 처음 발견한 시각 (n8n 기록에 종료 시각이 없을 때 대신 씀)


def notify(title, text):
    """맥 알림 (맥이 아니면 그냥 넘어감)"""
    try:
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def failed_execution_ids(key):
    data = recover.n8n("/executions?status=error&limit=20", key)
    return [str(e["id"]) for e in data.get("data", [])]


def build(ex_id, key):
    try:
        execution = recover.n8n(f"/executions/{ex_id}?includeData=true", key)
        workflow = recover.n8n(f"/workflows/{execution['workflowId']}", key)
        plan = recover.make_plan(execution, workflow, min_age=time.time() - first_seen.get(ex_id, time.time()))
        plan["workflow"] = workflow.get("name", execution["workflowId"])
        statuses = {x["status"] for x in plan["plan"]}
        if "다시 보내야 함" in statuses:
            plan["status"] = "승인 대기"
        elif "확인 대기" in statuses:
            plan["status"] = "확인 대기"       # 조회 결과가 늦게 반영될 수 있어 잠시 뒤 다시 확인
        elif "확인 불가" in statuses:
            plan["status"] = "사람 확인 필요"
        else:
            plan["status"] = "복구 필요 없음"
    except SystemExit as e:               # recover.py는 문제가 있으면 sys.exit으로 이유를 알려줌
        plan = {"workflow": "?", "node": "?", "status": "자동 복구 불가", "reason": str(e), "plan": []}
    except Exception as e:
        plan = {"workflow": "?", "node": "?", "status": "자동 복구 불가", "reason": repr(e), "plan": []}
    plan["id"] = ex_id
    plan["found_at"] = datetime.now().strftime("%H:%M:%S")
    plan["results"] = []
    return plan


def execute(plan):
    """승인된 계획 실행. 보내기 직전에 한 번 더 확인하고, 잡기·기록은 recover.send_items가 맡음."""
    todo = []
    skipped = 0
    for x in plan["plan"]:
        if x["status"] != "다시 보내야 함":
            continue
        if recover.lookup(plan["service"], x["id"]) is True:
            skipped += 1
            plan["results"].append(f"{x['id']}: 그사이 이미 처리됨 → 건너뜀")
            continue
        todo.append(x)
    r = recover.send_items(todo, plan, plan["id"], worker="watch")
    plan["results"] += r["lines"]
    plan["status"] = "복구 완료" if r["failed"] == 0 else "일부 확인 필요"
    plan["summary"] = (f"보낸 요청 {r['sent']}건 성공, {r['failed']}건 응답 실패, "
                       f"건너뜀 {skipped + r['skipped_claimed']}건. 실제 중복 여부는 상대 서비스 기록으로 확인하세요.")


def poll_loop(key):
    while True:
        try:
            with lock:
                waiting = [i for i, p in plans.items() if p["status"] == "확인 대기"]
            for ex_id in waiting:                      # 기다린 뒤 다시 조회해서 계획 갱신
                plan = build(ex_id, key)
                with lock:
                    plans[ex_id] = plan
                if plan["status"] != "확인 대기":
                    print(f"[{datetime.now():%H:%M:%S}] 실행 {ex_id} 다시 확인 → {plan['status']}")
                    if plan["status"] == "승인 대기":
                        notify("Agent Recovery", f"{plan['workflow']}: 복구 승인 필요")
            for ex_id in failed_execution_ids(key):
                if ex_id in seen:
                    continue
                seen.add(ex_id)
                first_seen[ex_id] = time.time()
                plan = build(ex_id, key)
                with lock:
                    plans[ex_id] = plan
                todo = sum(x["status"] == "다시 보내야 함" for x in plan["plan"])
                done = sum(x["status"] == "이미 처리됨" for x in plan["plan"])
                print(f"[{plan['found_at']}] 실패 발견: 실행 {ex_id} ({plan['workflow']}) → {plan['status']}"
                      f" (이미 처리됨 {done}, 다시 보낼 것 {todo})")
                if plan["status"] == "승인 대기":
                    notify("Agent Recovery", f"{plan['workflow']}: {todo}건 복구 승인 필요")
                elif plan["status"] == "복구 필요 없음":
                    notify("Agent Recovery", f"{plan['workflow']}: 실패로 보이지만 이미 처리됨")
        except SystemExit as e:
            print(f"n8n 확인 중 문제: {e}")
        except Exception as e:
            print(f"n8n 확인 중 문제: {e!r}")
        time.sleep(POLL_SECONDS)


STYLE = """body{font-family:-apple-system,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#222}
.card{border:1px solid #ddd;border-radius:10px;padding:16px;margin:12px 0}
.s{display:inline-block;padding:2px 8px;border-radius:6px;font-size:13px;background:#eee}
.wait{background:#fff3cd}.done{background:#d1f0d8}.bad{background:#f8d7da}
button{font-size:15px;padding:6px 16px;border-radius:6px;border:1px solid #888;cursor:pointer;margin-right:8px}
.ok{background:#1f7a3a;color:#fff;border-color:#1f7a3a} small{color:#666}"""


def page():
    with lock:
        items = sorted(plans.values(), key=lambda p: int(p["id"]) if p["id"].isdigit() else 0, reverse=True)
    out = [f"<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='5'>"
           f"<title>Agent Recovery</title><style>{STYLE}</style></head><body>",
           "<h2>Agent Recovery 승인 페이지</h2>",
           f"<small>10초마다 n8n을 확인해요. 이 페이지는 5초마다 새로고침돼요. 감시 시작 후 실패 {len(items)}건</small>"]
    if not items:
        out.append("<div class='card'>아직 새로 실패한 실행이 없어요.</div>")
    for p in items:
        e = html.escape
        cls = {"승인 대기": "wait", "복구 완료": "done", "복구 필요 없음": "done"}.get(p["status"], "bad")
        done = [x for x in p["plan"] if x["status"] == "이미 처리됨"]
        todo = [x for x in p["plan"] if x["status"] == "다시 보내야 함"]
        unknown = [x for x in p["plan"] if x["status"] == "확인 불가"]
        out.append(f"<div class='card'><b>{e(p['workflow'])}</b> <small>실행 {e(p['id'])} · {e(p['found_at'])} 발견</small><br>"
                   f"<span class='s {cls}'>{e(p['status'])}</span>")
        if p.get("reason"):
            out.append(f"<p>{e(p['reason'])}</p>")
        else:
            out.append(f"<p>실패한 노드: <b>{e(p['node'])}</b><br>n8n 에러: {e(str(p.get('error')))}</p>"
                       f"<p>항목 {len(p['plan'])}개를 하나씩 확인한 결과<br>"
                       f"· 이미 처리됨 <b>{len(done)}</b>개 → 다시 보내지 않음<br>"
                       f"· 다시 보내야 함 <b>{len(todo)}</b>개"
                       + (f" ({e(', '.join(str(x['id']) for x in todo[:5]))}{' …' if len(todo) > 5 else ''})" if todo else "")
                       + (f"<br>· 확인 불가 <b>{len(unknown)}</b>개 → 안전을 위해 보내지 않음" if unknown else "")
                       + f"<br><small>n8n 기본 Retry였다면 {len(p['plan'])}건을 전부 다시 보냈을 거예요.</small></p>")
            if p.get("downstream"):
                out.append(f"<p><small>주의: 다음 노드 {e(', '.join(p['downstream']))}는 v0.1에서 이어서 실행하지 않아요.</small></p>")
        if p["status"] == "승인 대기":
            out.append(f"<form method='post' action='/plans/{e(p['id'])}/approve' style='display:inline'>"
                       f"<button class='ok'>승인: {len(todo)}건 보내기</button></form>"
                       f"<form method='post' action='/plans/{e(p['id'])}/reject' style='display:inline'>"
                       f"<button>거절</button></form>")
        if p.get("summary"):
            out.append(f"<p><b>{e(p['summary'])}</b></p>")
        out.append("</div>")
    out.append("</body></html>")
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._html(page())
        self._html("없는 주소", 404)

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "plans" and parts[2] in ("approve", "reject"):
            with lock:
                p = plans.get(parts[1])
                if p and p["status"] == "승인 대기":
                    p["status"] = "실행 중" if parts[2] == "approve" else "거절함"
                    go = parts[2] == "approve"
                else:
                    go = False
            if go:
                execute(p)
                print(f"[{datetime.now():%H:%M:%S}] 실행 {p['id']} 승인 → {p['summary']}")
            elif p and p["status"] == "거절함":
                print(f"[{datetime.now():%H:%M:%S}] 실행 {p['id']} 거절 → 아무것도 보내지 않음")
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        self._html("없는 주소", 404)


def main():
    key = recover.load_api_key()
    seen.update(failed_execution_ids(key))      # 이미 있던 실패는 건드리지 않음
    print(f"Agent Recovery 감시 시작 (기존 실패 {len(seen)}건은 무시)")
    print(f"승인 페이지: http://127.0.0.1:{PORT}   (끄려면 Ctrl + C)")
    threading.Thread(target=poll_loop, args=(key,), daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
