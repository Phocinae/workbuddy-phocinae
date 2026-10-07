#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-phocinae · 斑海豹决策核心（纯 Python 标准库，零第三方依赖）

职责：把 WorkBuddy 传来的办公上下文转换为斑海豹后端请求

    POST /v1/systemone
    {"model": "Phocinae-Largha-150M-v1", "state": "<上下文>", "questions": [...]}

并解析答案、应用阈值，产出 typed 判定 JSON。
后端不可达 / 超时 / 响应异常 / 答案非法 → fail-closed 降级（安全默认值）。

问题类型：noul=bool；choice=int 下标（options 内）；score=2..10。
环境变量：PHOCINAE_ENDPOINT（默认 http://127.0.0.1:8155）
          PHOCINAE_API_KEY （可选鉴权，留空不鉴权）
          PHOCINAE_TIMEOUT （秒，默认 10）
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:8155"
DEFAULT_MODEL = "Phocinae-Largha-150M-v1"
DEFAULT_TIMEOUT = 10.0
MAX_STATE_CHARS = 8000   # 斑海豹 8k 上下文，超出强制裁剪
SYSTEMONE_PATH = "/v1/systemone"
CONNECTOR_VERSION = "0.1.0"


class BackendError(Exception):
    """后端不可达 / 超时 / 非 200 / 响应不可解析 / 答案不合法。"""


# --------------------------------------------------------------------------
# 四个决策点的提问模板、阈值与 fail-closed 安全默认值
# --------------------------------------------------------------------------
KIND_SPECS = {
    "doc_classify": {
        "title": "文档分类",
        "summary": "判断文档类别（合同/发票/报销单/报告/邮件/其他），用于批量归档与文件整理；"
                   "置信度不足时只作建议并转人工。",
        "questions": [
            {"id": "category", "type": "choice",
             "options": ["合同", "发票", "报销单", "报告", "邮件", "其他"]},
        ],
        "confidence_threshold": 0.60,
        "fail_closed": {
            "category": None,
            "action": "hold_for_review",
            "reason": "斑海豹服务不可用或响应异常，fail-closed：文档转人工分类",
        },
    },
    "approval_predict": {
        "title": "审批预判",
        "summary": "预判审批事项是否可批准并给出风险分（2-10）；"
                   "风险>=8 或判定不批准即拒绝，6<=风险<=7 转人工复核。",
        "questions": [
            {"id": "approve", "type": "noul"},
            {"id": "risk", "type": "score", "threshold": 5},
        ],
        "confidence_threshold": 0.70,
        "fail_closed": {
            "decision": "deny",
            "risk": None,
            "reason": "斑海豹服务不可用或响应异常，fail-closed：默认不批准，转人工复核",
        },
    },
    "task_route": {
        "title": "任务路由",
        "summary": "为任务选择执行路由（文档处理流水线/财务专家/法务专家/通用助理/人工）；"
                   "模型判定不可自动化的任务一律路由到人工。",
        "questions": [
            {"id": "route_target", "type": "choice",
             "options": ["文档处理流水线", "财务专家", "法务专家", "通用助理", "人工"]},
            {"id": "is_automated", "type": "noul"},
        ],
        "confidence_threshold": 0.65,
        "fail_closed": {
            "route": "人工",
            "automated": False,
            "reason": "斑海豹服务不可用或响应异常，fail-closed：任务路由至人工",
        },
    },
    "quality_gate": {
        "title": "质量 gate",
        "summary": "对 AI 产物（PPT/文档/报表等）做交付前质量把关；质量分<6、判定不通过"
                   "或置信不足均不放行。",
        "questions": [
            {"id": "pass_gate", "type": "noul"},
            {"id": "quality_score", "type": "score", "threshold": 6},
        ],
        "confidence_threshold": 0.70,
        "fail_closed": {
            "pass": False,
            "quality_score": None,
            "issues": ["斑海豹服务不可用，质量门 fail-closed：不予放行"],
            "reason": "fail-closed",
        },
    },
}


def _call_backend(state, questions, endpoint, timeout, api_key):
    """POST /v1/systemone，异常一律抛 BackendError。"""
    payload = {"model": DEFAULT_MODEL, "state": state, "questions": questions}
    url = endpoint.rstrip("/") + SYSTEMONE_PATH
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "workbuddy-phocinae/" + CONNECTOR_VERSION,
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise BackendError("后端 HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise BackendError("后端不可达: %s" % e.reason)
    except OSError as e:
        raise BackendError("请求 IO 错误/超时: %s" % e)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise BackendError("响应非合法 JSON: %s" % e)


def _parse_answers(kind, obj):
    """校验 answers 与 answer_confidence；任何不合法 → BackendError（fail-closed）。"""
    if not isinstance(obj, dict):
        raise BackendError("响应不是 JSON 对象")
    spec = KIND_SPECS[kind]
    answers = obj.get("answers")
    if not isinstance(answers, dict):
        raise BackendError("响应缺少 answers 字段")
    out = {}
    for q in spec["questions"]:
        qid, qtype = q["id"], q["type"]
        if qid not in answers:
            raise BackendError("答案缺少问题 %s" % qid)
        v = answers[qid]
        if qtype == "noul":
            if not isinstance(v, bool):
                raise BackendError("问题 %s 答案应为 bool，实际 %s" % (qid, type(v).__name__))
        elif qtype == "score":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise BackendError("问题 %s 答案应为数值，实际 %s" % (qid, type(v).__name__))
            if not (2 <= v <= 10):
                raise BackendError("问题 %s 分值越界: %s" % (qid, v))
        elif qtype == "choice":
            if isinstance(v, bool) or not isinstance(v, int):
                raise BackendError("问题 %s 答案应为 int 下标，实际 %s" % (qid, type(v).__name__))
            if not (0 <= v < len(q.get("options", []))):
                raise BackendError("问题 %s 下标越界: %s" % (qid, v))
        out[qid] = v
    conf = obj.get("answer_confidence")
    if isinstance(conf, dict):
        vals = [conf.get(q["id"]) for q in spec["questions"]]
        if any(v is None for v in vals):
            raise BackendError("answer_confidence 缺少本决策点问题的置信度")
        conf = min(float(v) for v in vals)
    elif isinstance(conf, (int, float)) and not isinstance(conf, bool):
        conf = float(conf)
    else:
        raise BackendError("缺少 answer_confidence")
    if not (0.0 <= conf <= 1.0):
        raise BackendError("置信度越界: %s" % conf)
    return out, conf


def _apply_decision(kind, answers, conf):
    """在合法答案之上应用阈值策略，产出最终 typed 判定。"""
    spec = KIND_SPECS[kind]
    if kind == "doc_classify":
        category = spec["questions"][0]["options"][answers["category"]]
        if conf < spec["confidence_threshold"]:
            return {"category": category, "action": "review", "confidence": conf,
                    "note": "置信度低于 %.2f，仅作分类建议，需人工确认" % spec["confidence_threshold"]}
        return {"category": category, "action": "auto_archive", "confidence": conf}
    if kind == "approval_predict":
        approve, risk = answers["approve"], answers["risk"]
        if not approve or risk >= 8:
            decision = "deny"
        elif 6 <= risk <= 7:
            decision = "manual_review"
        else:
            decision = "approve"
        if decision == "approve" and conf < spec["confidence_threshold"]:
            decision = "manual_review"
        return {"decision": decision, "approve": approve, "risk": risk,
                "confidence": conf,
                "policy": "risk<=5 自动通过；6<=risk<=7 人工复核；risk>=8 拒绝"}
    if kind == "task_route":
        target = spec["questions"][0]["options"][answers["route_target"]]
        automated = bool(answers["is_automated"])
        if not automated:
            return {"route": "人工", "automated": False, "confidence": conf,
                    "note": "模型判定不可自动化，路由至人工"}
        if conf < spec["confidence_threshold"]:
            return {"route": "人工", "automated": False, "confidence": conf,
                    "note": "置信度低于 %.2f，路由至人工复核" % spec["confidence_threshold"]}
        return {"route": target, "automated": True, "confidence": conf}
    if kind == "quality_gate":
        pg, qs = answers["pass_gate"], answers["quality_score"]
        issues = []
        if not pg:
            issues.append("模型判定：质量门未通过")
        if qs < 6:
            issues.append("质量分 %s 低于阈值 6" % qs)
        if conf < spec["confidence_threshold"]:
            issues.append("判定置信度 %.2f 低于阈值 %.2f" % (conf, spec["confidence_threshold"]))
        return {"pass": not issues, "quality_score": qs, "confidence": conf,
                "issues": issues}
    raise ValueError("未知决策点: " + kind)


def decide(kind, context, endpoint=None, timeout=None, api_key=None):
    """对外主入口：上下文 → 判定 dict（含 fail-closed 降级）。"""
    if kind not in KIND_SPECS:
        raise ValueError("未知决策点: " + kind)
    spec = KIND_SPECS[kind]
    endpoint = (endpoint or os.environ.get("PHOCINAE_ENDPOINT") or DEFAULT_ENDPOINT).strip()
    timeout = (timeout if timeout is not None
               else float(os.environ.get("PHOCINAE_TIMEOUT", DEFAULT_TIMEOUT)))
    api_key = api_key if api_key is not None else os.environ.get("PHOCINAE_API_KEY", "")
    state = (context or "").strip()
    truncated = len(state) > MAX_STATE_CHARS
    state = state[:MAX_STATE_CHARS]

    t0 = time.time()
    base = {
        "kind": kind,
        "title": spec["title"],
        "model": DEFAULT_MODEL,
        "endpoint": endpoint,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        obj = _call_backend(state, spec["questions"], endpoint, timeout, api_key)
        answers, conf = _parse_answers(kind, obj)
    except BackendError as e:
        decision = dict(spec["fail_closed"])
        decision["degraded"] = True
        decision["degraded_reason"] = str(e)
        base.update({
            "mode": "fail_closed",
            "decision": decision,
            "latency_ms": round((time.time() - t0) * 1000),
            "state_truncated": truncated,
        })
        return base
    base.update({
        "mode": "model",
        "answers": answers,
        "answer_confidence": conf,
        "confidence_threshold": spec["confidence_threshold"],
        "usage": obj.get("usage"),
        "backend_model": obj.get("model", DEFAULT_MODEL),
        "decision": _apply_decision(kind, answers, conf),
        "latency_ms": round((time.time() - t0) * 1000),
        "state_truncated": truncated,
    })
    return base


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="斑海豹决策 CLI：上下文 → POST /v1/systemone → typed 判定（fail-closed 降级）")
    ap.add_argument("--kind", choices=sorted(KIND_SPECS), help="决策点类型")
    ap.add_argument("--context", help="上下文文本（与 --context-file 二选一）")
    ap.add_argument("--context-file", help="从文件读取上下文")
    ap.add_argument("--endpoint", default=os.environ.get("PHOCINAE_ENDPOINT", DEFAULT_ENDPOINT),
                    help="斑海豹服务地址（默认 %(default)s）")
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("PHOCINAE_TIMEOUT", DEFAULT_TIMEOUT)),
                    help="请求超时秒数（默认 %(default)s）")
    ap.add_argument("--list-kinds", action="store_true", help="列出全部决策点并退出")
    args = ap.parse_args(argv)

    if args.list_kinds:
        for k in sorted(KIND_SPECS):
            print("%-18s %s" % (k, KIND_SPECS[k]["title"]))
        return 0
    if not args.kind:
        ap.error("需要 --kind 或 --list-kinds")
    if args.context_file:
        with open(args.context_file, encoding="utf-8") as fh:
            ctx = fh.read()
    else:
        ctx = args.context
    if not ctx or not ctx.strip():
        ap.error("需要 --context 或 --context-file 提供非空上下文")
    result = decide(args.kind, ctx, endpoint=args.endpoint, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
