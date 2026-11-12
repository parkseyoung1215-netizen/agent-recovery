"""회귀 테스트 - n8n 커뮤니티에서 지적받은 경우들이 다시 깨지지 않게 막기

실행: python3 -m unittest discover -s tests -v   (저장소 맨 위 폴더에서)

지적 출처: https://community.n8n.io/t/327579
  - 확실히 안 써진 항목만 자동으로 다시 보낼 수 있어야 한다
  - 써졌는데 타임아웃 난 항목은, 조회가 늦거나 이상하면 다시 보내면 안 된다
  - 조회 응답이 {} 나 {"exists": "false"} 같으면 판단하지 말아야 한다
  - 두 작업자가 같은 항목을 동시에 보내면 안 된다
  - 대기 시간(settle)보다 조회가 더 늦게 반영되는 경우를 테스트로 드러내야 한다
  - "중복 0건" 같은 결과를 확인 없이 출력하면 안 된다
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import recover  # noqa: E402
from recover import decide  # noqa: E402


class DecideRules(unittest.TestCase):
    def test_strong_lookup_confirms_no_write_after_503(self):
        self.assertEqual(decide("rejected", False, 0, "strong", 30), "다시 보내야 함")

    def test_strong_lookup_waits_after_timeout(self):
        # 타임아웃 직후엔 요청이 아직 처리 중일 수 있음
        self.assertEqual(decide("timeout", False, 1, "strong", 30), "확인 대기")
        self.assertEqual(decide("timeout", False, 31, "strong", 30), "다시 보내야 함")

    def test_eventual_not_found_never_replayed_automatically(self):
        self.assertEqual(decide("rejected", False, 1, "eventual", 30), "확인 대기")
        self.assertEqual(decide("rejected", False, 31, "eventual", 30), "확인 불가")
        self.assertEqual(decide("timeout", False, 999, "eventual", 30), "확인 불가")

    def test_found_is_done(self):
        self.assertEqual(decide("timeout", True, 0, "eventual", 30), "이미 처리됨")

    def test_unknown_lookup_goes_to_human(self):
        self.assertEqual(decide("rejected", None, 999, "strong", 30), "확인 불가")

    def test_idempotency_key_on_first_attempt_allows_replay(self):
        self.assertEqual(decide("timeout", None, 0, "eventual", 30, keyed_first_attempt=True), "다시 보내야 함")


class _LookupStub(BaseHTTPRequestHandler):
    bodies = {"/empty": b"{}", "/string": b'{"exists": "false"}', "/null": b'{"exists": null}',
              "/false": b'{"exists": false}', "/true": b'{"exists": true}', "/nested": b'{"data": {"found": true}}'}

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = self.bodies.get(self.path.split("?")[0], b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class LookupParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _LookupStub)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def svc(self, path, exists="exists"):
        return {"lookup": f"http://127.0.0.1:{self.port}{path}?id={{id}}", "exists": exists}

    def test_malformed_lookups_are_unknown(self):
        for path in ("/empty", "/string", "/null"):
            with self.subTest(path=path):
                self.assertIsNone(recover.lookup(self.svc(path), "a@example.com"))

    def test_real_booleans_are_trusted(self):
        self.assertIs(recover.lookup(self.svc("/false"), "x"), False)
        self.assertIs(recover.lookup(self.svc("/true"), "x"), True)
        self.assertIs(recover.lookup(self.svc("/nested", "data.found"), "x"), True)

    def test_malformed_lookup_with_timed_out_write_is_not_replayed(self):
        found = recover.lookup(self.svc("/empty"), "x")
        self.assertEqual(decide("timeout", found, 999, "strong", 30), "확인 불가")


class Claims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        recover.STATE_DIR = Path(self.tmp.name) / "claims"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_one_of_many_concurrent_workers_wins(self):
        wins = []
        barrier = threading.Barrier(10)

        def worker(n):
            barrier.wait()
            if recover.claim("exec-1:url:customer20", worker=f"w{n}"):
                wins.append(n)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(wins), 1)

    def test_crash_after_send_leaves_sending_record(self):
        job = "exec-2:url:customer20"
        self.assertTrue(recover.claim(job))
        recover.mark(job, "sending")
        # 여기서 멈췄다고 가정: 다음 실행은 다시 잡지 못하고 기록을 봐야 함
        self.assertFalse(recover.claim(job))
        self.assertEqual(recover.claim_state(job), "sending")


class RestartAfterSending(unittest.TestCase):
    """작업자가 'sending'을 기록하고 결과를 남기기 전에 재시작되면, 다시 보내지 않고 사람에게 넘겨야 한다.
    (Clearwork Desk 제안. 주의: 잠금 파일은 같은 컴퓨터·같은 폴더를 쓰는 작업자끼리만 효과가 있음)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        recover.STATE_DIR = Path(self.tmp.name) / "claims"
        self.sent = []
        recover_http = recover.http

        def fake_http(method, url, body=None, headers=None, timeout=15):
            if method == "POST":
                self.sent.append(body)
                return 200, {}
            return 200, {"exists": False}      # 조회는 '없음' (아직 반영 안 됨)
        recover.http = fake_http
        self.addCleanup(setattr, recover, "http", recover_http)
        self.addCleanup(recover.find_service.__globals__.__setitem__, "find_service", recover.find_service)
        recover.find_service.__globals__["find_service"] = lambda url: {
            "id_field": "to", "lookup": "http://x/check?to={id}", "exists": "exists",
            "lookup_mode": "strong", "settle_seconds": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def _execution(self):
        items = [{"json": {"to": "customer20@example.com"}}]
        return {"id": "77", "stoppedAt": "2000-01-01T00:00:00Z", "data": {"resultData": {
            "lastNodeExecuted": "send", "error": {"message": "timeout"},
            "runData": {"src": [{"data": {"main": [items]}}], "send": [{"source": [{"previousNode": "src"}]}]}}}}

    def _workflow(self):
        return {"nodes": [{"name": "send", "type": "n8n-nodes-base.httpRequest", "parameters": {
            "method": "POST", "url": "http://x/email", "jsonBody": "={{ JSON.stringify({ to: $json.to }) }}"}}],
            "connections": {}}

    def test_restarted_worker_does_not_resend_and_flags_for_human(self):
        plan = recover.make_plan(self._execution(), self._workflow(), ex_id="77")
        self.assertEqual(plan["plan"][0]["status"], "다시 보내야 함")
        job = f"77:http://x/email:customer20@example.com"
        recover.claim(job, worker="first")
        recover.mark(job, "sending")             # 보냈을 수도 있는 순간에 재시작됐다고 가정
        plan = recover.make_plan(self._execution(), self._workflow(), ex_id="77")
        self.assertEqual(plan["plan"][0]["status"], "확인 불가")
        self.assertIn("sending", plan["plan"][0]["note"])
        r = recover.send_items([x for x in plan["plan"] if x["status"] == "다시 보내야 함"], plan, "77")
        self.assertEqual((r["sent"], self.sent), (0, []))


    def test_request_is_recorded_before_it_is_sent(self):
        """BorkoB 질문: 사람이 보는 건 ID뿐인가, 보내기 전 요청 그대로인가 → 보내기 전에 요청을 남긴다"""
        seen = {}
        recover_http = recover.http

        def peek(method, url, body=None, headers=None, timeout=15):
            if method == "POST":   # 보내는 순간, 기록에는 이미 요청이 있어야 함
                seen.update(json.loads(recover._claim_path(job).read_text()))
            return recover_http(method, url, body, headers, timeout)
        plan = recover.make_plan(self._execution(), self._workflow(), ex_id="78")
        job = "78:http://x/email:customer20@example.com"
        recover.http, inner = peek, recover.http
        try:
            recover_http = inner
            recover.send_items(plan["plan"], plan, "78")
        finally:
            recover.http = inner
        self.assertEqual(seen["state"], "sending")
        self.assertEqual(seen["request"]["body"], {"to": "customer20@example.com"})
        self.assertEqual(seen["request"]["headers"]["Idempotency-Key"], plan["plan"][0]["key"])

    def test_review_file_for_human_has_reason_request_and_its_source(self):
        recover.REVIEW_DIR = Path(self.tmp.name) / "review"
        job = "79:http://x/email:customer20@example.com"
        recover.claim(job)
        recover.mark(job, "sending", request={"method": "POST", "body": {"to": "customer20@example.com"}})
        plan = recover.make_plan(self._execution(), self._workflow(), ex_id="79")
        path = recover.write_review(plan, "79")
        rec = json.loads(path.read_text())[0]
        self.assertEqual(rec["id"], "customer20@example.com")
        self.assertIn("sending", rec["why_not_sent"])
        self.assertEqual(rec["first_attempt_request"]["body"], {"to": "customer20@example.com"})
        self.assertIn("다시 만든 것", rec["first_attempt_request"]["source"])
        self.assertEqual(rec["recovery_attempt"]["request"]["body"], {"to": "customer20@example.com"})

class FakeShopIntegration(unittest.TestCase):
    """실험 7 상황: customer10은 503(안 써짐), customer20은 써졌지만 타임아웃, 조회는 8초 늦게 반영."""

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "experiments" / "fake_shop.py")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:8000/state", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        recover.STATE_DIR = Path(self.tmp.name) / "claims"

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self):
        urllib.request.urlopen("http://127.0.0.1:8000/scenario/7", timeout=5)
        failed = []

        def one(i):
            to = f"customer{i}@example.com"
            req = urllib.request.Request("http://127.0.0.1:8000/email", method="POST",
                                         data=json.dumps({"order_id": to, "to": to}).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=3)
            except urllib.error.HTTPError:
                failed.append((to, "rejected"))
            except Exception:
                failed.append((to, "timeout"))
        threads = [threading.Thread(target=one, args=(i,)) for i in range(1, 51)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return failed

    def _state(self):
        return json.loads(urllib.request.urlopen("http://127.0.0.1:8000/state", timeout=5).read())

    def _recover(self, mode, settle):
        failed = self._batch()
        svc = {"lookup": "http://127.0.0.1:8000/check/email?to={id}", "exists": "exists"}
        time.sleep(settle)
        todo = []
        for to, failure in failed:
            if decide(failure, recover.lookup(svc, to), settle, mode, settle) == "다시 보내야 함":
                todo.append({"id": to, "body": {"order_id": to, "to": to}, "key": f"email-{to}"})
        plan = {"url": "http://127.0.0.1:8000/email", "method": "POST"}
        report = recover.send_items(todo, plan, ex_id="test")
        time.sleep(9)                     # 조회 반영이 끝난 뒤 실제 기록 확인
        st = self._state()
        dup = st["중복"]["메일"]["중복된_건수"] if st["중복"] != "없음" else 0
        return report, st["메일_발송"]["총_발송"], dup

    def test_lag_longer_than_settle_does_not_cause_duplicate_when_configured_eventual(self):
        report, total, dup = self._recover("eventual", 3)
        self.assertEqual(dup, 0)
        self.assertEqual(report["sent"], 0)        # 확신할 수 없어서 아무것도 자동으로 안 보냄
        self.assertEqual(total, 49)                # customer10은 사람에게 넘어감 (누락은 사람이 처리)

    def test_assumption_violated_if_service_misconfigured_as_strong(self):
        # 대기 시간보다 늦게 반영되는 서비스를 strong으로 잘못 설정하면 중복이 생긴다.
        # 이 테스트는 그 가정을 드러내기 위한 것: 설정이 틀리면 도구도 틀린다.
        report, total, dup = self._recover("strong", 3)
        self.assertEqual(report["sent"], 2)
        self.assertEqual(dup, 1)


if __name__ == "__main__":
    unittest.main()
