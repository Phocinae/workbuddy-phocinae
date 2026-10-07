#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-phocinae 自测（fail-closed 分支）

运行前提：斑海豹后端（http://127.0.0.1:8155）未启动 —— 本测试专门验证
后端不可用时的 fail-closed 降级行为与配置文件完整性。纯 CPU，无任何外部依赖。

    python3 tests/self_test.py
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import phocinae_decision as pd  # noqa: E402

SAMPLE1 = os.path.join(ROOT, "tests", "samples", "sample1_invoice.txt")
SAMPLE2 = os.path.join(ROOT, "tests", "samples", "sample2_expense.txt")
ENDPOINT = "http://127.0.0.1:8155"  # 假定后端未启动


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestConfigFiles(unittest.TestCase):
    """全部 JSON 配置可解析，且结构符合 WorkBuddy 连接器规范的关键要求。"""

    def test_all_json_parse(self):
        for name in ("connector-meta.json", "mcp.json", "token-schema.json"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                json.load(fh)  # 解析失败即抛异常

    def test_connector_meta_fields(self):
        with open(os.path.join(ROOT, "connector-meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        for key in ("name", "name_en", "description", "description_zh",
                    "description_en", "source", "type", "version",
                    "examples_zh", "examples_en", "auth_mode", "minWorkbuddyVersion"):
            self.assertIn(key, meta)
        self.assertEqual(meta["source"], "workbuddy-phocinae")
        self.assertEqual(meta["type"], "mcp")
        self.assertEqual(meta["auth_mode"], "token")
        # source 必须为小写字母、数字、连字符
        self.assertRegex(meta["source"], r"^[a-z0-9-]+$")

    def test_mcp_json_single_stdio_server(self):
        with open(os.path.join(ROOT, "mcp.json"), encoding="utf-8") as fh:
            mcp = json.load(fh)
        servers = mcp["mcpServers"]
        self.assertEqual(len(servers), 1)  # 一个连接器只配一个 MCP Server
        name, cfg = next(iter(servers.items()))
        self.assertEqual(name, "workbuddy-phocinae")
        self.assertEqual(cfg["type"], "stdio")
        self.assertIn("scripts/phocinae_mcp.py", cfg["args"])
        # 不得硬编码真实凭证：env 值必须是 ${VAR} 占位
        for v in cfg.get("env", {}).values():
            self.assertRegex(v, r"^\$\{[A-Z_]+\}$")
        # 占位符与 token-schema 表单字段一一对应
        with open(os.path.join(ROOT, "token-schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        keys = {f["key"] for f in schema["fields"]}
        self.assertEqual(set(cfg["env"]), keys)

    def test_token_schema_fields(self):
        with open(os.path.join(ROOT, "token-schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        for key in ("title", "description", "fields"):
            self.assertIn(key, schema)
        self.assertGreaterEqual(len(schema["fields"]), 1)
        for f in schema["fields"]:
            for key in ("key", "label", "type", "required"):
                self.assertIn(key, f)
            self.assertIn(f["type"], ("text", "password"))
            if f["type"] == "password":  # 敏感字段一律 password
                self.assertTrue(f["key"].endswith(("_KEY", "_TOKEN", "_SECRET"))
                                or f["key"] == "PHOCINAE_API_KEY")


class TestFailClosed(unittest.TestCase):
    """后端不可用 → 四个决策点全部按安全默认值降级。"""

    def _expect_degraded(self, kind, context):
        r = pd.decide(kind, context, endpoint=ENDPOINT, timeout=2.0)
        self.assertEqual(r["mode"], "fail_closed")
        self.assertTrue(r["decision"]["degraded"])
        self.assertIn("degraded_reason", r["decision"])
        self.assertLess(r["latency_ms"], 30000)
        return r

    def test_defaults(self):
        self.assertEqual(pd.DEFAULT_ENDPOINT, "http://127.0.0.1:8155")
        self.assertEqual(pd.DEFAULT_MODEL, "Phocinae-Largha-150M-v1")
        self.assertEqual(pd.MAX_STATE_CHARS, 8000)

    def test_doc_classify_sample1(self):
        """样例1（发票）：后端不可用 → hold_for_review，不自动归档。"""
        r = self._expect_degraded("doc_classify", _read(SAMPLE1))
        self.assertEqual(r["decision"]["action"], "hold_for_review")
        self.assertIsNone(r["decision"]["category"])

    def test_approval_predict_sample2(self):
        """样例2（报销单）：后端不可用 → deny，绝不默认放行。"""
        r = self._expect_degraded("approval_predict", _read(SAMPLE2))
        self.assertEqual(r["decision"]["decision"], "deny")

    def test_task_route_fail_closed(self):
        r = self._expect_degraded("task_route", "整理本周所有报销单并归档")
        self.assertEqual(r["decision"]["route"], "人工")
        self.assertFalse(r["decision"]["automated"])

    def test_quality_gate_fail_closed(self):
        r = self._expect_degraded("quality_gate", "季度经营分析报告（30 页）")
        self.assertFalse(r["decision"]["pass"])
        self.assertTrue(r["decision"]["issues"])

    def test_state_truncation_flag(self):
        long_ctx = "发票 " * 5000  # >8000 字符
        r = self._expect_degraded("doc_classify", long_ctx)
        self.assertTrue(r["state_truncated"])


class TestInputValidation(unittest.TestCase):
    """入参与未知决策点的防御。"""

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            pd.decide("no_such_kind", "x", endpoint=ENDPOINT, timeout=1.0)

    def test_empty_context(self):
        r = pd.decide("doc_classify", "   ", endpoint=ENDPOINT, timeout=1.0)
        self.assertEqual(r["mode"], "fail_closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
