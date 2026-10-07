#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-phocinae 成功路径 round-trip 测试（本地 mock 后端，纯 CPU）

在本地起一个 mock 斑海豹后端（优先 127.0.0.1:8155，被占用则随机端口），
验证：请求构造（POST /v1/systemone、model、questions、8k 裁剪）、答案解析、
阈值判定、低置信/非法答案/HTTP 500 等异常场景。测试结束自动关闭 mock 服务。

    python3 tests/mock_backend_test.py
"""

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import phocinae_decision as pd  # noqa: E402

SAMPLE1 = os.path.join(ROOT, "tests", "samples", "sample1_invoice.txt")

# 按问题 id 给出的模拟答案（无场景标记时）
CANNED = {
    "category": 1,          # 发票
    "approve": True,
    "risk": 4,
    "route_target": 2,      # 法务专家
    "is_automated": True,
    "pass_gate": True,
    "quality_score": 8,
}

LAST_REQUEST = {}


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        LAST_REQUEST["path"] = self.path
        LAST_REQUEST["headers"] = dict(self.headers)
        LAST_REQUEST["body"] = json.loads(raw.decode("utf-8"))
        state = LAST_REQUEST["body"].get("state", "")
        if "@http500" in state:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        answers = {}
        for q in LAST_REQUEST["body"].get("questions", []):
            qid = q["id"]
            if "@bad_answer" in state and qid == "category":
                answers[qid] = 99          # 非法 choice 下标
            else:
                answers[qid] = CANNED[qid]
        if "@conf_low" in state:
            conf = 0.40
        elif "@conf_dict" in state:
            conf = {q["id"]: 0.85 for q in LAST_REQUEST["body"].get("questions", [])}
        else:
            conf = 0.90
        resp = {
            "model": pd.DEFAULT_MODEL,
            "answers": answers,
            "usage": {"tokens": 512, "latency_ms": 18},
            "answer_confidence": conf,
        }
        body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默
        pass


def _free_port(preferred):
    if preferred:
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port(int(os.environ.get("MOCK_PORT", "8155")))
        cls.endpoint = "http://127.0.0.1:%d" % cls.port
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _decide(self, kind, context):
        return pd.decide(kind, context, endpoint=self.endpoint, timeout=5.0)

    def test_request_shape(self):
        r = self._decide("doc_classify", "一张增值税发票")
        self.assertEqual(r["mode"], "model")
        self.assertEqual(LAST_REQUEST["path"], "/v1/systemone")
        body = LAST_REQUEST["body"]
        self.assertEqual(body["model"], "Phocinae-Largha-150M-v1")
        self.assertEqual(body["questions"][0],
                         {"id": "category", "type": "choice",
                          "options": ["合同", "发票", "报销单", "报告", "邮件", "其他"]})

    def test_doc_classify_sample1_auto_archive(self):
        with open(SAMPLE1, encoding="utf-8") as fh:
            r = self._decide("doc_classify", fh.read())
        self.assertEqual(r["mode"], "model")
        self.assertEqual(r["decision"]["category"], "发票")
        self.assertEqual(r["decision"]["action"], "auto_archive")
        self.assertEqual(r["answer_confidence"], 0.90)

    def test_doc_classify_low_confidence_review(self):
        r = self._decide("doc_classify", "@conf_low 一张发票")
        self.assertEqual(r["decision"]["action"], "review")
        self.assertEqual(r["decision"]["category"], "发票")

    def test_approval_predict_approve(self):
        r = self._decide("approval_predict", "差旅报销单，符合标准")
        self.assertEqual(r["decision"]["decision"], "approve")
        self.assertEqual(r["decision"]["risk"], 4)

    def test_task_route(self):
        r = self._decide("task_route", "审阅供应商合同")
        self.assertEqual(r["decision"]["route"], "法务专家")
        self.assertTrue(r["decision"]["automated"])

    def test_quality_gate_pass(self):
        r = self._decide("quality_gate", "季度报告终稿")
        self.assertTrue(r["decision"]["pass"])
        self.assertEqual(r["decision"]["issues"], [])

    def test_bad_answer_fail_closed(self):
        r = self._decide("doc_classify", "@bad_answer 一张发票")
        self.assertEqual(r["mode"], "fail_closed")
        self.assertTrue(r["decision"]["degraded"])

    def test_http500_fail_closed(self):
        r = self._decide("approval_predict", "@http500 报销单")
        self.assertEqual(r["mode"], "fail_closed")
        self.assertEqual(r["decision"]["decision"], "deny")

    def test_conf_dict_supported(self):
        r = self._decide("doc_classify", "@conf_dict 一张发票")
        self.assertEqual(r["mode"], "model")
        self.assertEqual(r["answer_confidence"], 0.85)

    def test_state_truncation_to_8k(self):
        long_ctx = "合同条款 " * 3000  # >8000 字符
        r = self._decide("doc_classify", long_ctx)
        self.assertTrue(r["state_truncated"])
        self.assertLessEqual(len(LAST_REQUEST["body"]["state"]), pd.MAX_STATE_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
