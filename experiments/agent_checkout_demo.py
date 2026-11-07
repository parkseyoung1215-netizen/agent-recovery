"""AI 비서 결제 실험 - 피클 같은 '대신 결제해 주는 AI 비서'가 실패하면?

n8n 없이, AI 비서가 쇼핑몰에서 결제하는 상황을 흉내 내요.
가짜 쇼핑몰(fake_shop.py)이 켜져 있어야 해요.

두 가지 비서를 비교해요.
  흔한 비서:  응답이 안 오면 "실패했나 보다" 하고 최대 3번 다시 결제
  안전한 비서: 응답이 안 오면 먼저 "이 주문 결제됐어?"를 확인
             → 이미 됐으면 멈추고 "결제 완료"라고 보고
             → 안 됐으면 사용자 승인을 받고, 중복 방지 키를 붙여서 1번만 다시

두 가지 상황에서 시험해요.
  실험 4: 결제는 처리됐는데 응답이 10초 늦음 (비서는 5초만 기다림)
  실험 5: 결제 서버가 1번 503 에러 (실제로 처리 안 됨)

실행: python3 agent_checkout_demo.py
"""
import json
import socket
import urllib.error
import urllib.request

SHOP = "http://127.0.0.1:8000"
WAIT_SECONDS = 5


def call(method, path, body=None, headers=None, timeout=WAIT_SECONDS):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SHOP + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        return "timeout", {}


def naive_agent(order):
    log = []
    for attempt in range(1, 4):
        code, _ = call("POST", "/pay", order)
        log.append(f"결제 시도 {attempt}: {code}")
        if code == 200:
            return "결제 성공", log
    return "결제 실패 (사용자에게 실패라고 보고)", log


def safe_agent(order, approve=lambda msg: True):
    log = []
    key = f"pay-{order['order_id']}"
    code, _ = call("POST", "/pay", order, headers={"Idempotency-Key": key})
    log.append(f"결제 시도 1: {code}")
    if code == 200:
        return "결제 성공", log
    # 실패처럼 보이면, 다시 하기 전에 먼저 확인
    _, res = call("GET", f"/check/pay?order_id={order['order_id']}", timeout=15)
    log.append(f"결제됐는지 확인: {'이미 결제됨' if res.get('exists') else '결제 안 됨'}")
    if res.get("exists"):
        return "결제 완료 확인 (다시 결제하지 않음)", log
    if not approve(f"{order['order_id']} 결제가 안 됐어요. 다시 결제할까요?"):
        return "사용자가 거절 (결제 안 함)", log
    log.append("사용자 승인 받음")
    code, _ = call("POST", "/pay", order, headers={"Idempotency-Key": key})
    log.append(f"결제 시도 2: {code}")
    return ("결제 성공" if code == 200 else "결제 실패 (사용자에게 보고)"), log


def run(scenario, agent_name, agent):
    call("GET", f"/scenario/{scenario}", timeout=15)
    order = {"order_id": f"order-s{scenario}", "amount": 39000}
    report, log = agent(order)
    _, st = call("GET", "/state", timeout=20)
    paid = st.get("결제_횟수", {}).get(order["order_id"], 0)
    return {"실험": scenario, "비서": agent_name, "비서의 보고": report,
            "실제 결제 횟수": paid, "과정": log}


def main():
    rows = []
    for scenario in (4, 5):
        for name, agent in (("흔한 비서", naive_agent), ("안전한 비서", safe_agent)):
            r = run(scenario, name, agent)
            rows.append(r)
            print(f"[실험 {scenario}] {name}: 보고='{r['비서의 보고']}', 실제 결제 {r['실제 결제 횟수']}번")
            for line in r["과정"]:
                print(f"    - {line}")
    print("\n| 실험 | 비서 | 비서의 보고 | 실제 결제 횟수 |")
    print("|---|---|---|---|")
    for r in rows:
        print(f"| {r['실험']} | {r['비서']} | {r['비서의 보고']} | {r['실제 결제 횟수']}번 |")


if __name__ == "__main__":
    main()
