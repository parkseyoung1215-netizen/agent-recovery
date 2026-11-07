"""가짜 쇼핑몰 서버 (Agent Recovery 실험용)

맥북 안에서만 돌아가는 가짜 결제·재고·배송·메일 서비스예요.
진짜 돈이나 메일은 하나도 안 나가요.

실행:   python3 fake_shop.py
상태:   브라우저에서 http://127.0.0.1:8000/state

실험 고르기 (브라우저 주소창에 입력하면 기록이 지워지고 그 실험 모드가 됨)
  /scenario/1  배송이 처음 1번 503 에러 (일시 장애)
  /scenario/2  배송은 실제로 처리되지만 응답이 10초 늦음 (n8n은 타임아웃으로 실패 처리)
  /scenario/3  메일 일괄 발송 중 33번째 요청에서 1번 503 에러

요청 헤더에 Idempotency-Key를 넣으면, 같은 키로 온 요청은 한 번만 처리해요.
"""
import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8000
SCENARIOS = {
    1: "배송 일시 장애 (처음 1번 503)",
    2: "배송 처리는 되지만 응답이 10초 늦음 (타임아웃처럼 보임)",
    3: "메일 일괄 발송 중 33번째에서 1번 503",
    4: "결제는 처리되지만 응답이 10초 늦음 (AI 비서 결제 실험)",
    5: "결제 서버가 1번 503 (처리 안 됨, AI 비서 결제 실험)",
    6: "메일 50건 중 33번째 1번 503 + 조회 기능 없음 (중복 방지 키만 지원)",
}

state = {}


def reset(scenario=1):
    state.clear()
    state.update({
        "scenario": scenario,
        "payments": [],
        "inventory_changes": [],
        "shipments": [],
        "emails": [],
        "ship_failures_left": 1 if scenario == 1 else 0,
        "ship_delay_seconds": 10 if scenario == 2 else 0,
        "email_fail_at": 33 if scenario in (3, 6) else 0,
        "no_check": scenario == 6,
        "pay_delay_seconds": 10 if scenario == 4 else 0,
        "pay_failures_left": 1 if scenario == 5 else 0,
        "email_requests": 0,
        "seen_keys": {},
    })


def count(rows, key):
    c = {}
    for r in rows:
        c[r[key]] = c.get(r[key], 0) + 1
    return c


def summary():
    pay = count(state["payments"], "order_id")
    ship = count(state["shipments"], "order_id")
    mail = count(state["emails"], "to")
    dup = {}
    for name, c in (("결제", pay), ("배송", ship), ("메일", mail)):
        d = {k: v for k, v in c.items() if v > 1}
        if d:
            dup[name] = {"중복된_건수": len(d), "예시": dict(list(d.items())[:3])}
    return {
        "실험": f"{state['scenario']}번 - {SCENARIOS[state['scenario']]}",
        "결제_횟수": pay,
        "재고_차감_횟수": count(state["inventory_changes"], "order_id"),
        "배송_횟수": ship,
        "메일_발송": {"총_발송": len(state["emails"]), "받은_사람_수": len(mail)},
        "중복": dup if dup else "없음",
        "기록": {
            "payments": state["payments"],
            "inventory_changes": state["inventory_changes"],
            "shipments": state["shipments"],
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            print("  (상대가 먼저 연결을 끊음 - 하지만 처리는 이미 완료됨)")

    def log_message(self, fmt, *args):
        print(f"[{datetime.now():%H:%M:%S}] {self.command} {self.path}")

    def do_GET(self):
        if self.path.startswith("/state"):
            return self._send(200, summary())
        if self.path.startswith("/scenario/"):
            try:
                n = int(self.path.rstrip("/").split("/")[-1])
                assert n in SCENARIOS
            except (ValueError, AssertionError):
                return self._send(400, {"error": "1, 2, 3 중에 고르세요"})
            reset(n)
            return self._send(200, {"ok": True, "실험": f"{n}번 - {SCENARIOS[n]}", "message": "기록 초기화 완료"})
        if self.path.startswith("/check/") and state.get("no_check"):
            return self._send(404, {"error": "이 서비스는 처리 여부 조회를 지원하지 않음"})
        if self.path.startswith("/check/"):
            # 실제 서비스의 "주문 조회", "발송 내역 조회" 같은 기능
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            kind = u.path.split("/")[-1]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            table = {"pay": ("payments", "order_id"), "inventory": ("inventory_changes", "order_id"),
                     "ship": ("shipments", "order_id"), "email": ("emails", "to")}
            if kind not in table:
                return self._send(404, {"error": "조회할 수 없는 종류"})
            rows_name, field = table[kind]
            value = q.get(field)
            if not value:
                return self._send(400, {"error": f"{field}가 필요함"})
            found = [r for r in state[rows_name] if r.get(field) == value]
            return self._send(200, {"exists": bool(found), "count": len(found)})
        if self.path.startswith("/reset"):
            reset(state.get("scenario", 1))
            return self._send(200, {"ok": True, "message": "초기화 완료"})
        return self._send(404, {"error": "없는 주소"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "JSON 형식이 아님"})
        order_id = body.get("order_id")
        if not order_id:
            return self._send(400, {"error": "order_id가 없음"})

        key = self.headers.get("Idempotency-Key")
        if key and key in state["seen_keys"]:
            return self._send(200, {**state["seen_keys"][key], "중복_요청_무시": True})

        now = datetime.now().isoformat(timespec="seconds")
        if self.path == "/pay":
            if state.get("pay_failures_left", 0) > 0:
                state["pay_failures_left"] -= 1
                return self._send(503, {"error": "Service Unavailable: 결제 서버 일시 장애"})
            state["payments"].append({"order_id": order_id, "amount": body.get("amount"), "at": now})
            result = {"ok": True, "step": "결제", "order_id": order_id}
            if key:
                state["seen_keys"][key] = result
            if state.get("pay_delay_seconds"):
                print(f"  결제 처리 완료. 응답은 {state['pay_delay_seconds']}초 뒤에 보냄")
                time.sleep(state["pay_delay_seconds"])
            return self._send(200, result)
        elif self.path == "/inventory":
            state["inventory_changes"].append({"order_id": order_id, "item": body.get("item"), "at": now})
            result = {"ok": True, "step": "재고", "order_id": order_id}
        elif self.path == "/ship":
            if state["ship_failures_left"] > 0:
                state["ship_failures_left"] -= 1
                return self._send(503, {"error": "Service Unavailable: 배송 서버 일시 장애"})
            state["shipments"].append({"order_id": order_id, "at": now})
            result = {"ok": True, "step": "배송", "order_id": order_id}
            if key:
                state["seen_keys"][key] = result
            if state["ship_delay_seconds"]:
                print(f"  배송 처리 완료. 응답은 {state['ship_delay_seconds']}초 뒤에 보냄")
                time.sleep(state["ship_delay_seconds"])
            return self._send(200, result)
        elif self.path == "/email":
            state["email_requests"] += 1
            if state["email_fail_at"] and state["email_requests"] == state["email_fail_at"]:
                return self._send(503, {"error": "Service Unavailable: 메일 서버 일시 장애"})
            to = body.get("to") or order_id
            state["emails"].append({"to": to, "order_id": order_id, "at": now})
            result = {"ok": True, "step": "메일", "to": to}
        else:
            return self._send(404, {"error": "없는 주소"})

        if key:
            state["seen_keys"][key] = result
        return self._send(200, result)


if __name__ == "__main__":
    reset(1)
    print(f"가짜 쇼핑몰 실행 중: http://127.0.0.1:{PORT}/state  (끄려면 Ctrl + C)")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
