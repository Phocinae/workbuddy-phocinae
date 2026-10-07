#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-phocinae · MCP stdio 服务（最小 JSON-RPC 2.0 实现，纯 Python 标准库）

协议：stdin/stdout 上换行分隔的 JSON-RPC 2.0（MCP stdio 传输）。
暴露 4 个工具（对应四个决策点）：
    phocinae_doc_classify   文档分类
    phocinae_approval_predict  审批预判
    phocinae_task_route     任务路由
    phocinae_quality_gate   质量 gate
所有工具共用入参 {context: str(必填), notes?: str(可选)}，
返回 typed 判定 JSON；后端不可用时 fail-closed 降级（见 phocinae_decision）。
日志/调试信息一律写 stderr，stdout 只输出 JSON-RPC 消息。
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import phocinae_decision as pd  # noqa: E402

SERVER_NAME = "workbuddy-phocinae"
SERVER_VERSION = pd.CONNECTOR_VERSION
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2024-11-05"


def _tool_def(kind):
    spec = pd.KIND_SPECS[kind]
    return {
        "name": "phocinae_" + kind,
        "description": "%s。%s" % (spec["title"], spec["summary"]),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "待决策上下文，不超过 %d 字符（超出自动裁剪）。" % pd.MAX_STATE_CHARS,
                },
                "notes": {
                    "type": "string",
                    "description": "可选补充说明，会拼入上下文。",
                },
            },
            "required": ["context"],
        },
    }


TOOLS = [_tool_def(k) for k in sorted(pd.KIND_SPECS)]


def _tool_result(req_id, content, is_error=False, structured=None):
    result = {"content": content, "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle(msg):
    """处理一条 JSON-RPC 消息，返回响应（None 表示无需响应）。"""
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        proto = params.get("protocolVersion")
        if proto not in SUPPORTED_PROTOCOLS:
            proto = DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = (params.get("name") or "")
        kind = name[len("phocinae_"):] if name.startswith("phocinae_") else None
        args = params.get("arguments") or {}
        if kind not in pd.KIND_SPECS:
            return _tool_result(mid, [{"type": "text", "text": "未知工具: %s" % name}],
                                is_error=True)
        context = args.get("context")
        if not isinstance(context, str) or not context.strip():
            return _tool_result(mid, [{"type": "text",
                                       "text": "缺少必填参数 context（待决策上下文）"}],
                                is_error=True)
        ctx = context
        if args.get("notes"):
            ctx = context + "\n\n【补充说明】" + str(args["notes"])
        try:
            decision = pd.decide(kind, ctx)
        except Exception as e:  # noqa: BLE001 —— MCP 层自身兜底，避免进程崩溃
            return _tool_result(mid, [{"type": "text",
                                       "text": "决策执行异常: %s" % e}], is_error=True)
        text = json.dumps(decision, ensure_ascii=False, indent=2)
        return _tool_result(mid, [{"type": "text", "text": text}], structured=decision)
    if not method:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "未知方法: %s" % method}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # 忽略非 JSON 行，保持协议鲁棒
        resp = handle(msg)
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
