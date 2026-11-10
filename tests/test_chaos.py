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
        urls = {n["name"]: n["parameters"]["url"] for n in copy["nodes"]}
        self.assertEqual(urls["pay"], "http://127.0.0.1:8200/sink/https/api.example.com/pay?x=1")
        self.assertEqual(urls["read"], "https://api.example.com/items")
        self.assertEqual(urls["dyn"], "={{ $json.url }}")
        self.assertEqual((len(changed), len(skipped)), (1, 1))
        self.assertTrue(copy["name"].startswith("[chaos test]"))
        self.assertNotIn("active", copy)
        self.assertEqual(wf["nodes"][0]["parameters"]["url"], "https://api.example.com/pay?x=1")  # 원본은 그대로


if __name__ == "__main__":
    unittest.main()
