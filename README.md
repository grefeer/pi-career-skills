# pi-py — AI Agent 工具集（Python）

> 本仓库的旗舰应用是求职多 Agent 评测运行时 **pi-career-skills**；底座是 5 个对齐上游 [Pi](https://pi.dev) 的 SDK 包（统一 LLM API / agent 循环 / 编码工具 / 存储 / 服务化）。

[Pi](https://github.com/earendil-works/pi)（作者 Mario Zechner，2026 年迁至 [Earendil Works](https://github.com/earendil-works)）是一套 TypeScript 的 AI agent 工具集。本仓库将其核心能力移植为 Python SDK，**以 SDK 库形式提供，不含 CLI/TUI**；并在此之上自研了求职多 Agent 评测运行时 `pi-career-skills`。

---

## 🚀 旗舰应用：pi-career-skills

**求职多 Agent 评测运行时** —— 输入一个求职问题 + 简历（URL / PDF / 文本），输出带证据链的求职结果（岗位发现 → 匹配 → 简历定制 → 职业规划），并逐条记录工具调用日志。

```
用户问题 + 简历
   │
   ▼
Supervisor（规划与委托）
   │  delegate-job-discovery / -matching / -resume-tailoring / -career-planning
   ▼
┌────────────────────────────────────────────────────────────────┐
│ 4 个技能子 agent（各只见自己的工具目录，技能隔离）                  │
│  job-discovery     职位发现      13 个工具（搜索/浏览/抓取/抽取/去重） │
│  job-matching      职位匹配       2 个工具（确定性匹配打分）          │
│  resume-tailoring  简历定制       2 个工具（生成定制简历要点）         │
│  career-planning   职业规划       2 个工具（生成求职准备计划）         │
└────────────────────────────────────────────────────────────────┘
   │
   ▼
Run 级 Harness：证据库 · 完成门 · 预算 · stall 检测 · 有界自动恢复 · 确定性终止
```

- **5 个 agent**：1 个 supervisor（只持有 4 个 `delegate-*` 委托工具）+ 4 个技能子 agent，通过 `pi-coding-agent` 的 `CodingAgent` 封装构造，共享预算/证据库/终止信号。
- **16 个确定性工具**：技能目录 `13 / 2 / 2 / 2`（含各技能共享的 `read-skill-reference`），均为纯函数业务逻辑，输入输出经 pydantic 双向校验，错误统一转为带稳定错误码的观察结果（脱敏，不抛异常）。
- **Run 级 harness**（[runtime/](packages/pi-career-skills/src/pi_career_skills/runtime/)）：证据库（source-backed，模型不可凭空造证据）、完成门（按技能交付契约判定）、预算（轮次 + 墙钟双维度）、stall 检测（连续无新证据时软引导收尾）、有界自动恢复、确定性终止、类型化委托契约。
- **安全约束（硬性）**：SSRF 防护（`_assert_public_url` + 每请求路由守卫 `_abort_non_public`）；同意闸门对 `验证码 / 登录 / 扫码 / 请在微信客户端打开` 等关键词硬拦截；渲染永不加载登录态 profile；不做登录/反爬绕过；微信 OCR 默认关闭。
- **模型**：通过 OpenAI 兼容协议调用 DeepSeek（`deepseek-v4-flash`，[model_factory.py](packages/pi-career-skills/src/pi_career_skills/model_factory.py)），`faux` 为免 key 冒烟模型。
- **钩子体系**：pi-ai → pi-agent-core → pi-coding-agent → pi-career-skills 四层 hook，业务实现见 [agent_hooks.py](packages/pi-career-skills/src/pi_career_skills/runtime/agent_hooks.py)（stream 计费、预算准入、证据提升、stall 引导、交付终止）。

### 快速运行

```bash
# 冒烟测试（无需 API key）
python main.py --model-id faux

# 真实运行（需 DEEPSEEK_API_KEY 环境变量）
python main.py
```

入口 [main.py](main.py)：`run_career_task(question, resume_url, ...)` 返回终态、摘要、证据库 artifacts 与完整事件流；工具调用日志逐条打印并追加写入 `temp/results/tool_calls.jsonl`。示例问题即 25 题评测之一（Q046）。

### 评测

- **25 题评测集**（[eval_results/questions/normalized/](eval_results/questions/normalized/)）：覆盖四技能 + 多步链式（chain）任务，含审计（audit）逐条核对证据与交付物。
- **评测 CLI**：`pi_career_skills.evaluation.cli` 支持单进程顺序执行（`--workers 1`）、审计、结果对比，每条记录原子写入 `<out>/<qid>.json`。

#### 最新评测结果（2026-08-29）

在最小实现重构（流水线 LangGraph → 纯循环）后复跑 25 题评测集，结果见 [eval_results/manifest25_20260828_222711/](eval_results/manifest25_20260828_222711/)：

| 指标 | 值 |
|---|---|
| 模型 / 并发 | `deepseek-v4-flash`，4 workers（playwright fallback） |
| 退出码 | 0（无崩溃，25/25 全部落盘） |
| 终态 | `succeeded` 27 个节点（17 题完全成功）；`waiting_user` 8 个节点 |

`waiting_user` 为受控收尾而非失败，保留原始状态：`completion_evidence_unavailable` ×4（C008, C014, Q046, R024）、`no_progress` ×3（C015, Q040, Q045）、`budget_exhausted` ×1（R025）。

### 学习文档

[`docs/study/pi-career-skills/`](docs/study/pi-career-skills/)：教案、架构对照、[hook 体系全景](docs/study/pi-career-skills/hook体系全景.md)（含 mermaid 图）、一次完整请求的旅程、run 方法调用流程图、五个维度分析（目标边界 / 感知记忆 / 工具生态 / 稳健性 / 评估闭环）。

---

## SDK 底座（对齐上游 Pi）

### 同步状态

- **当前对齐版本**：[`v0.84.1`](./UPSTREAM_VERSION)（2026-08-12，破例同步 patch）
- **同步策略**：仅在上游发布 `0.x.0`（minor）时集中同步，详见 [`SYNC.md`](./SYNC.md)

| 包 | 上游对应 | 状态 | 说明 |
|---|---|---:|---|
| [`pi-ai`](./packages/pi-ai) | `@earendil-works/pi-ai` | ✅ | 统一 LLM API（OpenAI + Anthropic + retry） |
| [`pi-agent-core`](./packages/pi-agent-core) | `@earendil-works/pi-agent-core` | ✅ | agent 循环引擎 + harness（技能/会话/压缩） |
| [`pi-storage-sqlite`](./packages/pi-storage-sqlite) | `@earendil-works/pi-storage-sqlite-node` | ✅ | SQLite 会话存储后端 |
| [`pi-coding-agent`](./packages/pi-coding-agent) | `@earendil-works/pi-coding-agent` | ✅ | 编码 agent SDK（bash/read/edit/write/grep/find/ls） |
| [`pi-server`](./packages/pi-server) | `@earendil-works/pi-server` | ✅ | agent 服务化（Unix socket + JSONL + supervisor） |
| [`pi-career-skills`](./packages/pi-career-skills) | —（自研旗舰） | ✅ | 求职多 Agent 评测运行时（见上文） |

### 安装

SDK 各包均已发布到 [PyPI](https://pypi.org/user/encyc/)（Python ≥ 3.11），可按需安装单个包，内部依赖会自动解析；`pi-career-skills` 目前从源码运行：

```bash
pip install pi-agent-core   # agent 运行时（含 pi-ai）
pip install pi-coding-agent # 编码 agent SDK
```

| PyPI 包 | 用途 |
|---|---|
| [`pi-py-ai`](https://pypi.org/project/pi-py-ai/) | 统一 LLM API（OpenAI + Anthropic + retry） |
| [`pi-py-agent-core`](https://pypi.org/project/pi-py-agent-core/) | agent 循环引擎 + harness |
| [`pi-py-storage-sqlite`](https://pypi.org/project/pi-py-storage-sqlite/) | SQLite 会话存储后端 |
| [`pi-py-coding-agent`](https://pypi.org/project/pi-py-coding-agent/) | 编码 agent SDK |
| [`pi-py-server`](https://pypi.org/project/pi-py-server/) | agent 服务化（Unix socket + JSONL） |

发布流程：打 tag 并创建 GitHub Release 后，[`publish.yml`](./.github/workflows/publish.yml) 自动构建并发布全部 5 个 SDK 包到 PyPI。发布记录见 [Releases](https://github.com/encyc/pi-py/releases) 与 [CHANGELOG.md](./CHANGELOG.md)。

### 快速上手

也可以直接从源码运行：

```bash
git clone https://github.com/grefeer/pi-career-skills.git
cd pi-career-skills
uv sync
```

#### 基础 LLM 调用

```python
import asyncio
from pi_ai import stream, Context, UserMessage, Model, StreamOptions

model = Model(
    id="deepseek-chat", api="openai-completions", provider="deepseek",
    base_url="https://api.deepseek.com/v1", input=["text"],
    context_window=64000, max_tokens=8192,
)

async def main():
    ctx = Context(messages=[UserMessage(content="你好")])
    es = stream(model, ctx, StreamOptions(api_key="sk-..."))
    async for event in es:
        if event.type == "text_delta":
            print(event.delta, end="")
    print()
    msg = await es.result()
    print(f"usage: {msg.usage.input} in / {msg.usage.output} out")

asyncio.run(main())
```

#### Agent + 工具调用

```python
import asyncio
from pi_ai import Model, TextContent, UserMessage
from pi_agent_core import Agent, AgentOptions, AgentToolResult

class WeatherTool:
    name = "get_weather"
    description = "查询天气"
    label = "Weather"
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
        return AgentToolResult(content=[TextContent(text=f"{params['city']} 晴天 25°C")])

async def main():
    agent = Agent(AgentOptions(
        initial_state={"system_prompt": "你是助手", "model": model, "tools": [WeatherTool()]},
        get_api_key=lambda p: "sk-...",
    ))
    agent.subscribe(lambda ev, sig: print(ev.type), )
    await agent.prompt("北京天气怎么样？")

asyncio.run(main())
```

#### 编码 Agent

```python
import asyncio
from pi_ai import Model
from pi_coding_agent import CodingAgent

async def main():
    agent = CodingAgent(
        model=model,
        api_key="sk-...",
        cwd=".",  # 工作目录
    )
    await agent.prompt("读取 README.md 并总结")

asyncio.run(main())
```

### 包结构与依赖

```
packages/
├── pi-ai/              # 叶子 — 统一 LLM API
├── pi-agent-core/      # → pi-ai — agent 运行时
├── pi-storage-sqlite/  # → pi-ai + pi-agent-core — SQLite 后端
├── pi-coding-agent/    # → pi-agent-core + pi-ai — 编码 agent SDK
├── pi-server/          # → pi-coding-agent — RPC 服务
└── pi-career-skills/   # → pi-coding-agent + pi-agent-core + pi-ai — 求职多 Agent 评测运行时（旗舰）
```

依赖方向自底向上，与上游一致。每个有意偏离上游的地方，记录在对应包的 [`PORTING.md`](./packages/pi-ai/PORTING.md) 中。

### 技术选型

| 领域 | 选型 | 对应上游 |
|---|---|---|
| 类型/校验 | Pydantic v2 | typebox |
| 异步 | asyncio + AsyncGenerator | Promise + ReadableStream |
| LLM provider | 各厂原生 Python SDK | 各厂原生 TS SDK |
| 存储 | stdlib `sqlite3` | node:sqlite |
| 包管理 | uv workspace | npm workspaces |

### 开发

```bash
uv sync                 # 安装全部依赖（含 dev）
uv run pytest           # 跑测试（默认跳过 integration）
uv run pytest -m integration  # 真实 LLM 调用测试（需 API key + 消耗额度）
uv run ruff check       # lint
uv run ruff format      # 格式化
uv run mypy             # 类型检查（strict）
```

集成测试需要设置环境变量（参考 [`.env`](./.env)）：
- `OPENAI_API_KEY` — OpenAI 测试
- `DEEPSEEK_API_KEY` — DeepSeek 测试（OpenAI 兼容协议，pi-career-skills 真实运行必需）
- `ANTHROPIC_API_KEY` — Anthropic 测试

贡献指南详见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)。

### 路线图

- [x] 5 包基线完成（对齐上游 v0.84.1）
- [x] OpenAI/DeepSeek provider 真实验证
- [x] Anthropic provider（纯逻辑测试，待真实 API 验证）
- [x] pi-career-skills：4 技能 + run 级 harness + 25 题评测
- [ ] Google / Mistral / Bedrock provider
- [ ] OAuth 鉴权（`auth/*`）
- [ ] 扩展系统（`extensions/`）

## 许可证

MIT，与上游保持一致。详见 [`LICENSE`](./LICENSE)。
