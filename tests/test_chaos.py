"""실패 주입 테스트(chaos.py)가 제대로 판정하는지 확인

n8n HTTP 노드의 동작을 흉내 냄:
  - 한 번만 실행: 항목들을 동시에 보내고, 하나라도 실패하면 노드 실패 (재시도 없음)
  - Retry On Fail: 노드가 실패하면 같은 항목 전체를 다시 보냄 (최대 3번)
"""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import chaos  # noqa: E402


def run_node(base, items, keyed, tries, timeout=3):
    """HTTP 노드 한 개를 흉내 냄. 모든 항목이 성공하면 True."""
    def one(to):
        headers = {"Content-Type": "application/json"}
        if keyed:
            headers["Idempotency-Key"] = f"order-confirm-{to}"
        req = urllib.request.Request(base + "https/api.example.com/email", method="POST",
                                     data=json.dumps({"to": to}).encode(), headers=headers)
        try:
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except Exception:
            return False
    for _ in range(tries):
        with ThreadPoolExecutor(max_workers=10) as ex:
            if all(ex.map(one, items)):
                return True
    return False


class ChaosVerdicts(unittest.TestCase):
    ITEMS = [f"customer{i}@example.com" for i in range(1, 51)]

    def start(self, **cfg):
        srv, sink = chaos.serve(port=0, **cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/sink/", sink

    def test_single_try_leaves_one_missing(self):
        base, sink = self.start(fail_nth=33, late_nth=0, late_seconds=0)
        run_node(base, self.ITEMS, keyed=False, tries=1)
        r = sink.report()
        self.assertEqual(r["판정"], "실패")
        self.assertEqual(len(r["누락 예시"]), 1)

    def test_retry_on_fail_without_key_duplicates_49(self):
        base, sink = self.start(fail_nth=33, late_nth=0, late_seconds=0)
        run_node(base, self.ITEMS, keyed=False, tries=3)
        r = sink.report()
        self.assertEqual(r["판정"], "실패")
        self.assertIn("중복 49건", r["문제"][0])

    def test_retry_on_fail_with_key_passes(self):
        base, sink = self.start(fail_nth=33, late_nth=0, late_seconds=0)
        run_node(base, self.ITEMS, keyed=True, tries=3)
        r = sink.report()
        self.assertEqual(r["판정"], "통과", r["문제"])
        self.assertEqual(r["중복 방지된 재시도"], 49)

    def test_late_reply_without_key_duplicates(self):
        base, sink = self.start(fail_nth=0, late_nth=20, late_seconds=5)
        run_node(base, self.ITEMS, keyed=False, tries=3, timeout=2)
        self.assertEqual(sink.report()["판정"], "실패")

    def test_late_reply_with_key_passes(self):
        base, sink = self.start(fail_nth=0, late_nth=20, late_seconds=5)
        run_node(base, self.ITEMS, keyed=True, tries=3, timeout=2)
        self.assertEqual(sink.report()["판정"], "통과")


class Prepare(unittest.TestCase):
    def test_rewrites_only_literal_write_urls(self):
        wf = {"name": "orders", "id": "x", "active": True, "nodes": [
            {"name": "pay", "type": "n8n-nodes-base.httpRequest", "parameters": {"method": "POST", "url": "https://api.example.com/pay?x=1"}},
            {"name": "read", "type": "n8n-nodes-base.httpRequest", "parameters": {"method": "GET", "url": "https://api.example.com/items"}},
            {"name": "dyn", "type": "n8n-nodes-base.httpRequest", "parameters": {"method": "POST", "url": "={{ $json.url }}"}},
        ], "connections": {}}
        copy, changed, skipped = chaos.prepare(wf)
        urls = {n["name"]: n["parameters"]["url"] for n in copy["nodes"] if "url" in n["parameters"]}
        self.assertEqual(urls["pay"], "http://127.0.0.1:8200/sink/https/api.example.com/pay?x=1")
        self.assertEqual(urls["read"], "https://api.example.com/items")
        self.assertEqual(urls["dyn"], "={{ $json.url }}")
        self.assertEqual((len(changed), len(skipped)), (1, 1))
        self.assertTrue(copy["name"].startswith("[chaos test]"))
        self.assertNotIn("active", copy)
        self.assertEqual(wf["nodes"][0]["parameters"]["url"], "https://api.example.com/pay?x=1")  # 원본은 그대로


class RunCommand(unittest.TestCase):
    """ar.py chaos run: 웹훅으로 워크플로를 실행하고 판정을 종료 코드로 돌려주는지"""

    def setUp(self):
        srv, self.sink = chaos.serve(fail_nth=33, late_nth=0, late_seconds=0, port=chaos.PORT)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

    def fake_n8n(self, keyed):
        """웹훅을 받으면 Retry On Fail 노드처럼 50건을 시험용 목적지로 보내는 가짜 n8n"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        items = ChaosVerdicts.ITEMS

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self.path.startswith("/webhook/chaos-"):
                    self.send_response(404)
                    self.end_headers()
                    return
                ok = run_node(chaos.SINK, items, keyed=keyed, tries=3)
                self.send_response(200 if ok else 500)
                self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_exit_codes(self):
        base = self.fake_n8n(keyed=False)
        self.assertEqual(chaos.run(base + "/webhook/chaos-B", settle=0), 1)
        base = self.fake_n8n(keyed=True)
        self.assertEqual(chaos.run(base + "/webhook/chaos-C", settle=0), 0)
        self.assertEqual(chaos.run(base + "/webhook-missing", settle=0), 2)

    def test_prepare_adds_webhook_wired_like_the_original_trigger(self):
        wf = {"id": "abc", "name": "orders", "nodes": [
            {"name": "start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
            {"name": "build", "type": "n8n-nodes-base.code", "parameters": {}},
        ], "connections": {"start": {"main": [[{"node": "build", "type": "main", "index": 0}]]}}}
        copy, _, _ = chaos.prepare(wf)
        hook = next(n for n in copy["nodes"] if n["name"] == "chaos test trigger")
        self.assertEqual(hook["parameters"]["path"], "chaos-abc")
        self.assertEqual(copy["connections"]["chaos test trigger"], wf["connections"]["start"])


class Contract(unittest.TestCase):
    """EKH1 지적: 가짜 목적지가 실제 API와 달라지면, 테스트는 통과하는데 실제로는 실패한다.
    → 진짜로 성공한 실행에서 요청 모양을 배워두고, 그와 다른 요청은 가짜 목적지가 거절한다."""

    WF = {"nodes": [{"name": "send", "type": "n8n-nodes-base.httpRequest", "parameters": {
        "method": "POST", "url": "https://api.example.com/email",
        "jsonBody": "={{ JSON.stringify({ to: $json.to, amount: $json.amount }) }}"}}], "connections": {}}

    def execution(self, ex_id, items, response):
        return {"id": ex_id, "data": {"resultData": {"runData": {
            "src": [{"data": {"main": [[{"json": x} for x in items]]}}],
            "send": [{"source": [{"previousNode": "src"}], "data": {"main": [[{"json": response}]]}}]}}}}

    def contract(self):
        exs = [self.execution("1", [{"to": "a@x.com", "amount": 3}], {"id": "msg_1", "status": "queued"}),
               self.execution("2", [{"to": "b@x.com", "amount": 5}], {"id": "msg_2", "status": "queued"})]
        return chaos.learn(self.WF, exs)

    def test_learns_fields_types_and_a_real_response(self):
        c = self.contract()["contracts"]["POST https/api.example.com/email"]
        self.assertEqual(c["requests"], 2)
        self.assertEqual(c["fields"]["to"]["types"], ["string"])
        self.assertEqual(c["fields"]["amount"]["types"], ["number"])
        self.assertEqual(c["response_example"], {"id": "msg_1", "status": "queued"})

    def test_sink_rejects_drifted_requests_and_fails_the_verdict(self):
        sink = chaos.Sink(fail_nth=0, late_nth=0, contract=self.contract())
        path = "/sink/https/api.example.com/email"
        self.assertEqual(sink.handle("POST", path, {}, json.dumps({"to": "c@x.com", "amount": 1}))[0], 200)
        self.assertEqual(sink.handle("POST", path, {}, json.dumps({"to": "d@x.com"}))[0], 400)                  # 필드 빠짐
        self.assertEqual(sink.handle("POST", path, {}, json.dumps({"to": "e@x.com", "amount": "1"}))[0], 400)   # 타입 바뀜
        self.assertEqual(sink.handle("POST", path, {}, json.dumps({"to": "f@x.com", "amount": 1, "cc": "g"}))[0], 400)  # 새 필드
        r = sink.report()
        self.assertEqual(r["판정"], "실패")
        self.assertTrue(any("계약 위반 3건" in p for p in r["문제"]))
        self.assertEqual(sink.response_for("POST", path), {"id": "msg_1", "status": "queued"})

    def test_contracts_from_two_workflows_merge_per_endpoint(self):
        a = self.contract()
        wf2 = json.loads(json.dumps(self.WF))
        wf2["nodes"][0]["name"] = "notify"
        wf2["nodes"][0]["parameters"]["jsonBody"] = "={{ JSON.stringify({ to: $json.to, amount: $json.amount, cc: $json.cc }) }}"
        ex = self.execution("3", [{"to": "z@x.com", "amount": 2, "cc": "y@x.com"}], {"id": "msg_3"})
        ex["data"]["resultData"]["runData"]["notify"] = ex["data"]["resultData"]["runData"].pop("send")
        merged = chaos.merge(a, chaos.learn(wf2, [ex]))
        c = merged["contracts"]["POST https/api.example.com/email"]
        self.assertEqual(c["requests"], 3)
        self.assertEqual(c["fields"]["cc"]["seen"], 1)            # 한 워크플로만 씀 → 필수 아님
        sink = chaos.Sink(fail_nth=0, late_nth=0, contract=merged)
        self.assertEqual(sink.handle("POST", "/sink/https/api.example.com/email", {}, json.dumps({"to": "q@x.com", "amount": 1}))[0], 200)
        self.assertEqual(sink.handle("POST", "/sink/https/api.example.com/email", {}, json.dumps({"to": "r@x.com", "amount": 1, "cc": "s"}))[0], 200)

    def test_without_contract_everything_is_accepted_as_before(self):
        sink = chaos.Sink(fail_nth=0, late_nth=0)
        self.assertEqual(sink.handle("POST", "/sink/https/api.example.com/email", {}, "{}")[0], 200)
        self.assertEqual(sink.report()["판정"], "통과")



class NoApiKey(unittest.TestCase):
    """n8n에서 내보낸 JSON 파일만으로 점검할 수 있어야 함 (API 키 없이 써보기 쉽게)"""

    def test_check_reads_exported_files(self):
        import check
        wfs = check.load_files([str(ROOT / "experiments" / "exp2_timeout_workflow.json")])
        self.assertEqual(wfs[0]["id"], "exp2_timeout_workflow")
        kinds = [f[2] for f in check.check_workflow(wfs[0], {})]
        self.assertTrue(any("HTTP" in k for k in kinds))

if __name__ == "__main__":
    unittest.main()
