---
name: workbuddy-phocinae
description: 斑海豹决策：文档分类、审批预判、任务路由、质量 gate。触发词：分类/归档/审批/预判/风险/路由/质量检查/交付前把关。
description_zh: 调用本地斑海豹决策模型，对办公上下文做四类结构化判定：文档分类、审批预判、任务路由、质量 gate；服务不可用时 fail-closed 降级。
description_en: Local Phocinae decision model for four structured office decisions: document classification, approval prediction, task routing, and quality gate, with fail-closed fallback.
version: 0.1.0
author: phocinae
---

# workbuddy-phocinae（斑海豹决策连接器技能）

本技能配合连接器 `workbuddy-phocinae` 使用，指导 AI 正确调用 4 个 MCP 工具。
所有工具均调用本地斑海豹决策模型（Phocinae-Largha-150M-v1，单次前向结构化决策，非生成式），
经 `POST /v1/systemone` 返回 typed 判定。工具不修改任何文件、无副作用，仅产出判定 JSON。

## 工具总览

| 决策点 | 工具名 | 典型触发场景 |
|---|---|---|
| 文档分类 | `phocinae_doc_classify` | 发票/合同/票据批量归档、云盘与收件箱文件整理 |
| 审批预判 | `phocinae_approval_predict` | 报销合规、合同风险分级、发布/发送前审批 |
| 任务路由 | `phocinae_task_route` | 专家团分发、技能选择、流水线选择 |
| 质量 gate | `phocinae_quality_gate` | AI 产物（PPT/文档/报表）交付前校验 |

**公共参数**：`context`（string，必填，待决策上下文，≤8000 字符，超出自动裁剪）、
`notes`（string，可选，补充说明）。

**返回结构**（所有工具一致）：
`{kind, title, model, endpoint, mode, decision{...}, answer_confidence, latency_ms, timestamp}`
- `mode: "model"` → 模型正常判定；`mode: "fail_closed"` → 服务不可用/超时/响应异常，已降级。
- 判定结果在 `decision` 字段；fail-closed 时 `decision.degraded == true` 并附带 `degraded_reason`。

## 决策点一：文档分类

- **工具**：`phocinae_doc_classify`
- **提问模板（发送给模型的 questions）**：
  - `category`：type=choice，options=[合同, 发票, 报销单, 报告, 邮件, 其他]
- **阈值**：`answer_confidence ≥ 0.60`
  - 达标 → `action: "auto_archive"`，按 `category` 自动归档；
  - 不足 → `action: "review"`，仅作分类建议，必须人工确认后再归档。
- **返回示例**：
  ```json
  {"category": "发票", "action": "auto_archive", "confidence": 0.87}
  ```

## 决策点二：审批预判

- **工具**：`phocinae_approval_predict`
- **提问模板**：
  - `approve`：type=noul（是否应批准）
  - `risk`：type=score，2–10，threshold=5（风险分）
- **判定规则**：
  - `risk ≤ 5` 且 `approve=true` → `approve`（自动通过）
  - `6 ≤ risk ≤ 7` → `manual_review`（人工复核）
  - `risk ≥ 8` 或 `approve=false` → `deny`（拒绝）
  - `approve` 时若 `answer_confidence < 0.70` → 降级为 `manual_review`
- **返回示例**：
  ```json
  {"decision": "manual_review", "approve": true, "risk": 6, "confidence": 0.81}
  ```

## 决策点三：任务路由

- **工具**：`phocinae_task_route`
- **提问模板**：
  - `route_target`：type=choice，options=[文档处理流水线, 财务专家, 法务专家, 通用助理, 人工]
  - `is_automated`：type=noul（能否自动执行）
- **判定规则**：
  - `is_automated=false` → 路由至「人工」；
  - `answer_confidence < 0.65` → 路由至「人工」复核；
  - 其余 → 按 `route_target` 路由。
- **返回示例**：
  ```json
  {"route": "法务专家", "automated": true, "confidence": 0.79}
  ```

## 决策点四：质量 gate

- **工具**：`phocinae_quality_gate`
- **提问模板**：
  - `pass_gate`：type=noul（是否通过质量门）
  - `quality_score`：type=score，2–10，threshold=6（质量分）
- **判定规则**：仅当 `pass_gate=true` 且 `quality_score ≥ 6` 且 `answer_confidence ≥ 0.70` 时 `pass=true`；
  否则 `pass=false` 并给出 `issues` 清单（未通过原因逐条列出）。
- **返回示例**：
  ```json
  {"pass": false, "quality_score": 5, "confidence": 0.88, "issues": ["质量分 5 低于阈值 6"]}
  ```

## 高风险操作确认规则（必须遵守）

- 审批预判：当 `decision=approve` 且 `risk ≥ 6`，或涉及资金支付、合同签署、对外发布时，
  必须先向用户展示判定依据并二次确认，不得直接执行。
- 审批预判：`decision=deny` / `manual_review` 的结果必须原样告知用户，不得擅自放行。
- 质量 gate：`pass=false` 时禁止交付产物；列出 `issues` 请用户修正。
- 任何 `mode="fail_closed"` 的结果都代表模型未参与判定（安全默认值），应明确告知用户
  「决策模型不可用，已按安全默认值处理」。
- 本连接器不执行任何写操作（不归档、不付款、不发布），执行动作始终由 WorkBuddy 主流程在
  判定结果之上显式进行。

## 认证与错误恢复

- **认证前置**：安装连接器时，WorkBuddy 会弹出「斑海豹服务对接配置」表单
  （token-schema.json），收集：服务地址（默认 `http://127.0.0.1:8155`）、可选访问令牌、超时秒数。
  凭证仅存用户本机 `~/.workbuddy`，不经过云端。
- **错误场景与恢复**：
  - 返回 `mode="fail_closed"` → 斑海豹服务未启动或不可达。提示用户启动本机斑海豹服务
    （监听 127.0.0.1:8155，路由 `POST /v1/systemone`），并检查表单中的服务地址与令牌。
  - 工具调用返回 `isError` 且提示「缺少必填参数 context」→ 重新组装上下文后调用。
  - 单次调用超过 10 秒（可配置）未返回 → 自动按 fail-closed 处理，无需等待。

## 调用示例（自然语言 → 工具）

1. 用户：「把桌面发票文件夹里的文件分一下类」→ 读取文件内容作为 `context`
   → `phocinae_doc_classify` → 按 `category` 归档，`action=review` 的先汇总给用户确认。
2. 用户：「这份报销单能批吗」→ 组装报销单内容 → `phocinae_approval_predict`
   → 按判定结果与「高风险操作确认规则」回复用户。
3. 用户：「这个任务交给谁做」→ 组装任务描述 → `phocinae_task_route` → 按 `route` 分发。
4. 用户：「帮我检查这份报告能不能交付」→ 组装报告摘要 → `phocinae_quality_gate`
   → `pass=false` 时列出 `issues`。
