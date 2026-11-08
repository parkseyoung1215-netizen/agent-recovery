"""실험 7·8 - 애매한 실패를 어떻게 다룰까 (n8n 커뮤니티 sameday의 제안)

메일 50건을 보내는데,
  - customer10: 서버가 503 → 실제로 안 보내짐
  - customer20: 실제로 보내졌지만 응답이 6초 늦음 → 3초 제한에 걸려 타임아웃
나머지 48건은 정상.

n8n HTTP 노드의 '항목별 에러 출력(On Error: Continue using error output)'처럼,
실패한 항목만 따로 모아서 실패 종류(503 / 타임아웃)와 함께 넘긴다.
그 다음 recover.py의 decide()로 각 항목을 판단한다.

  실험 7: 조회 결과가 8초 늦게 반영되는 서비스 (eventual)
  실험 8: 조회 결과가 바로 반영되는 서비스 (strong)

비교 대상:
  옛날 방식(v0.1): 조회해서 '없음'이면 바로 다시 보냄
  새 방식(v0.2):   타임아웃이거나 조회가 늦게 반영될 수 있으면, 일정 시간이 지난 뒤의 '없음'만 믿음

기대 결과 (sameday): 확실히 안 써진 항목만 다시 보낼 수 있고,
                   써졌는데 타임아웃 난 항목은 미해결(다시 보내지 않음)로 남아야 한다.

가짜 쇼핑몰(fake_shop.py)이 켜져 있어야 해요.
실행: python3 exp7_ambiguous_demo.py
"""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from recover import decide

SHOP = "http://127.0.0.1:8000"
CUSTOMERS = [f"customer{i}@example.com" for i in range(1, 51)]
CLIENT_TIMEOUT = 3
SETTLE = 10          # 이 서비스는 '없음'이 확정되기까지 10초면 충분하다고 설정


def call(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SHOP + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        return "timeout", {}


def send_batch():
    """50건 보내기. 실패한 항목을 (받는사람, 실패종류)로 돌려줌 = n8n 항목별 에러 출력"""
    def one(to):
        code, _ = call("POST", "/email", {"order_id": to, "to": to}, timeout=CLIENT_TIMEOUT)
        if code == 200:
            return None
        return (to, "timeout" if code == "timeout" else "rejected")
    with ThreadPoolExecutor(max_workers=10) as ex:
        return [r for r in ex.map(one, CUSTOMERS) if r]


def lookup(to):
    code, res = call("GET", "/check/email?" + urllib.parse.urlencode({"to": to}))
    return res.get("exists") if code == 200 else None


def run(scenario, mode, method):
    call("GET", f"/scenario/{scenario}")
    failed = send_batch()
    t0 = time.time()
    # 1차: 실패한 항목을 모두 바로 조회해서 판단
    first = {}
    for to, failure in failed:
        found = lookup(to)
        if method == "v0.1":
            first[to] = "이미 처리됨" if found else "다시 보내야 함"
        else:
            first[to] = decide(failure, found, time.time() - t0, lookup_mode=mode, settle=SETTLE)
    # 2차: '확인 대기'가 있으면 한 번만 기다렸다가 그 항목들만 다시 조회
    final = dict(first)
    waiting = [(to, f) for to, f in failed if first[to] == "확인 대기"]
    if waiting:
        time.sleep(max(0, SETTLE - (time.time() - t0)))
        for to, failure in waiting:
            final[to] = "확인 대기 → " + decide(failure, lookup(to), time.time() - t0, lookup_mode=mode, settle=SETTLE)
    rows = []
    for to, failure in failed:
        if final[to].endswith("다시 보내야 함"):
            call("POST", "/email", {"order_id": to, "to": to})
        rows.append((to.split("@")[0], failure, final[to]))
    time.sleep(1)
    _, st = call("GET", "/state")
    dup = st["중복"]["메일"]["중복된_건수"] if st["중복"] != "없음" else 0
    return rows, st["메일_발송"]["총_발송"], st["메일_발송"]["받은_사람_수"], dup


def main():
    print("| 실험 | 조회 반영 | 방식 | customer10 (503) | customer20 (타임아웃, 실제로는 보냄) | 총 발송 | 받은 사람 | 중복 |")
    print("|---|---|---|---|---|---|---|---|")
    for scenario, mode in ((7, "eventual"), (8, "strong")):
        for method in ("v0.1", "v0.2"):
            rows, total, people, dup = run(scenario, mode, method)
            d = {name: f"{status}" for name, failure, status in rows}
            print(f"| {scenario} | {'8초 늦게' if mode == 'eventual' else '바로'} | {method} "
                  f"| {d.get('customer10', '-')} | {d.get('customer20', '-')} | {total}통 | {people}명 | {dup}건 |")


if __name__ == "__main__":
    main()
