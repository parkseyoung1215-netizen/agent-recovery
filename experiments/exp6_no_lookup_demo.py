"""실험 6 - '이미 처리됐는지' 조회 기능이 없는 서비스에서는?

지금까지 recover.py는 상대 서비스에 "이거 처리됐어?"라고 물어볼 수 있어서 중복을 막았어요.
그런데 조회 기능이 없는 서비스도 많아요. 대신 Stripe처럼 '중복 방지 키(Idempotency-Key)'는
지원하는 서비스가 있어요. 같은 키로 두 번 보내면 두 번째는 무시하는 방식이에요.

상황: 메일 50건 발송 중 33번째 요청이 503 (실험 3과 같음). 단, 조회 기능 없음.
n8n이 하는 일을 흉내 냄: 50건을 차례로 보내고, 실패하면 Retry 때 50건을 전부 다시 보냄.

비교:
  A. 키 없이 보내고 → n8n Retry                     (지금 대부분의 워크플로)
  B. 키 없이 보내고 → recover.py 방식 (조회 시도)     (조회가 안 되면 어떻게 되나)
  C. 처음부터 키를 붙여 보내고 → n8n Retry 그대로     (check.py가 권하는 설정)

가짜 쇼핑몰(fake_shop.py)이 켜져 있어야 해요.
실행: python3 exp6_no_lookup_demo.py
"""
import json
import urllib.error
import urllib.parse
import urllib.request

SHOP = "http://127.0.0.1:8000"
CUSTOMERS = [f"customer{i}@example.com" for i in range(1, 51)]


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SHOP + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def send_all(with_key):
    """n8n의 HTTP 노드처럼 50건을 보내고, 하나라도 실패하면 노드 실패."""
    failed = []
    for to in CUSTOMERS:
        headers = {"Idempotency-Key": f"order-confirm-{to}"} if with_key else {}
        code, _ = call("POST", "/email", {"order_id": to, "to": to}, headers)
        if code != 200:
            failed.append(to)
    return failed


def state():
    _, st = call("GET", "/state")
    m = st["메일_발송"]
    dup = st["중복"]["메일"]["중복된_건수"] if st["중복"] != "없음" else 0
    return m["총_발송"], m["받은_사람_수"], dup


def case_a():
    call("GET", "/scenario/6")
    send_all(with_key=False)
    send_all(with_key=False)                       # n8n Retry: 노드 전체 다시
    return "키 없음 → n8n Retry", *state(), "성공으로 끝남"


def case_b():
    call("GET", "/scenario/6")
    send_all(with_key=False)
    unknown = 0
    for to in CUSTOMERS:                           # recover.py: 보내기 전에 하나씩 조회
        code, _ = call("GET", "/check/email?" + urllib.parse.urlencode({"to": to}))
        if code != 200:
            unknown += 1                           # 조회 불가 → 안전을 위해 보내지 않음
    return "키 없음 → recover.py 방식", *state(), f"조회 불가 {unknown}건 → 하나도 안 보내고 사람에게 넘김"


def case_c():
    call("GET", "/scenario/6")
    send_all(with_key=True)
    send_all(with_key=True)                        # n8n Retry 그대로, 하지만 같은 키
    return "처음부터 키 → n8n Retry", *state(), "서버가 49건을 '이미 받은 요청'으로 무시"


def main():
    print("| 방법 | 총 발송 | 받은 사람 | 중복 | 결과 |")
    print("|---|---|---|---|---|")
    for case in (case_a, case_b, case_c):
        name, total, people, dup, note = case()
        print(f"| {name} | {total}통 | {people}명 | {dup}건 | {note} |")


if __name__ == "__main__":
    main()
