# workbuddy-phocinae（斑海豹决策连接器）

WorkBuddy 办公智能体连接器：把本地**斑海豹决策模型**（Phocinae-Largha-150M-v1，单次前向结构化决策、非生成式）
接入 WorkBuddy，为其提供四类办公结构化决策能力。**未提交连接器市场**（自行取用）。

## 定位

- **是什么**：一个 MCP + Skill 型连接器（stdio 传输，本地进程），把 WorkBuddy 传来的办公上下文转换成
  `POST /v1/systemone` 请求，返回 typed 结构化判定（JSON），供 WorkBuddy 主模型在「工具/审批/路由」层使用。
- **不是什么**：不是生成式 LLM，不能当自定义模型位接入；不执行任何写操作（不归档、不付款、不发布）。
- **四个决策点**：

  | 决策点 | MCP 工具 | 场景 | 阈值/规则（详见 SKILL.md） |
  |---|---|---|---|
  | 文档分类 | `phocinae_doc_classify` | 发票/合同/票据批量归档 | 置信度 < 0.60 → 仅建议，转人工 |
  | 审批预判 | `phocinae_approval_predict` | 报销合规、合同风险分级 | risk≤5 通过；6–7 人工复核；≥8 拒绝 |
  | 任务路由 | `phocinae_task_route` | 专家团分发、流水线选择 | 置信度 < 0.65 或不可自动化 → 人工 |
  | 质量 gate | `phocinae_quality_gate` | 交付前质量把关 | 质量分 ≥6 且置信 ≥0.70 才放行 |

- **fail-closed 降级**：后端不可达/超时/响应异常/答案非法时按安全默认值降级——
  分类转人工、审批默认不批准、路由转人工、质量门不予放行。降级结果带 `degraded: true` 与 `degraded_reason`。

## 目录结构

```
workbuddy-phocinae/
├── connector-meta.json          # 连接器元信息（WorkBuddy 连接器规范）
├── mcp.json                     # MCP stdio 运行配置（一个连接器一个 MCP Server）
├── token-schema.json            # 用户自填 Token 表单（服务地址/令牌/超时）
├── icon.svg                     # 市场图标
├── skills/workbuddy-phocinae/
│   └── SKILL.md                 # 技能说明：四个决策点的提问模板与阈值
├── scripts/
│   ├── phocinae_decision.py     # 决策核心：上下文 → /v1/systemone → 判定（fail-closed）
│   └── phocinae_mcp.py          # MCP stdio 服务（纯标准库 JSON-RPC 2.0 实现）
├── tests/                       # 自测（含 mock 后端 round-trip 测试）
├── README.md
└── LICENSE                      # Apache-2.0
```

## 安装（Install）

前置条件：

1. **WorkBuddy 客户端 ≥ 4.23.0**（连接器使用 `auth_mode: "token"` 表单，自该版本起支持）；
2. **Python ≥ 3.8**（脚本仅用标准库，无第三方依赖；`python3` 需在 PATH 中，
   Windows 用户请将 `mcp.json` 中 `command` 改为 `python`）；
3. **斑海豹决策服务**运行在本机 `http://127.0.0.1:8155`（路由 `POST /v1/systemone`），
   或填写表单时指定你的私有部署地址。

本地使用（两种方式任选）：

- **方式一（连接器打包，面向审核上架）**：将本目录作为连接器包提交 WorkBuddy 团队审核后
  从连接器市场安装（本仓库当前未提交，见「上架说明」）。
- **方式二（个人本机直用，零审核）**：把以下配置合并进用户级 `~/.workbuddy/mcp.json` 的
  `mcpServers`，并把 `args` 指向本仓库 `scripts/phocinae_mcp.py` 的**绝对路径**：

  ```json
  {
    "mcpServers": {
      "workbuddy-phocinae": {
        "type": "stdio",
        "command": "python3",
        "args": ["/home/<你>/dev/workbuddy-phocinae/scripts/phocinae_mcp.py"],
        "env": {
          "PHOCINAE_ENDPOINT": "http://127.0.0.1:8155"
        }
      }
    }
  }
  ```

自测（无需 WorkBuddy）：

```bash
python3 tests/self_test.py            # fail-closed 分支（要求后端未启动）
python3 tests/mock_backend_test.py    # 成功路径 + 阈值 + 异常场景（本地 mock 后端）
python3 scripts/phocinae_decision.py --kind doc_classify --context "一张增值税发票……"
```

## 配置

- 连接器安装时 WorkBuddy 弹出「斑海豹服务对接配置」表单（token-schema.json），字段：
  - `PHOCINAE_ENDPOINT`：服务地址，默认 `http://127.0.0.1:8155`（必填）；
  - `PHOCINAE_API_KEY`：访问令牌，服务启用鉴权时填写（选填）；
  - `PHOCINAE_TIMEOUT`：请求超时秒数，默认 10（选填）。
- 环境变量同名可覆盖（`PHOCINAE_ENDPOINT` / `PHOCINAE_API_KEY` / `PHOCINAE_TIMEOUT`）。
- 单次请求超时 10 秒，远低于连接器规范建议的 30 秒响应上限。
- 上下文超过 8000 字符自动裁剪（斑海豹 8k 上下文上限），判定结果中以 `state_truncated` 标注。

## 安全声明

- **凭证仅存本机**：服务地址与访问令牌由 WorkBuddy 保存在用户本机 `~/.workbuddy`，
  不经过云端；本仓库不含任何真实凭证，`mcp.json` 仅用 `${VAR}` 占位。
- **数据不出本机**：上下文只发往表单中配置的服务地址（默认 `127.0.0.1:8155` 本机回环）。
- **最小权限**：连接器不读写文件、不执行命令，仅向用户配置的地址发起一次 POST 请求。
- **fail-closed**：模型不可用时自动按安全默认值降级，绝不静默放行（审批不默认通过、质量门不默认放行）。
- 斑海豹为非生成式结构化决策模型，判定结果仅为建议；高风险操作（付款/签署/发布）由 WorkBuddy
  主流程显式执行并二次确认（见 SKILL.md「高风险操作确认规则」）。

## License

Apache-2.0，见 [LICENSE](LICENSE)。模型权重许可另案管理（不在本仓库内）。

## 上架说明（当前状态）

- 本仓库**未提交** WorkBuddy 开放平台审核、**未上架**连接器市场、**未推送**任何远程仓库（本地 git 无 remote）。
- 后续如需公开分发：注册 open.workbuddy.cn → 资质认证 → 提交本目录（连接器打包）→ 平台审核 →
  上架连接器市场；远程 MCP 场景需另行提供 HTTPS 公网端点（本连接器为本地 stdio，不涉及）。
- 已知待核对项见仓库内验证记录：MCP 侧 `runtime` 字段官方目前仅支持 Node（本包经 `command: python3`
  直接启动，效果等同 Python 运行时，WorkBuddy 后续支持 `runtime: {type: python}` 后可切换）。
