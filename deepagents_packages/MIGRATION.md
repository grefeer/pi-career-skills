# 迁移方案：pi-py + hooks → deepagents + middleware

> 目标：在 **agent 结构不变**（薄 supervisor + 4 个 skill 专属子 agent）的前提下，
> 把 pi-career-skills 的 run 级 harness 从 pi-py 的 `AgentOptions` hooks 迁移到
> **deepagents 0.6.12 + langchain AgentMiddleware**，并尽量复用官方 18 个中间件。
> 前提：不参考源项目 langgraph-multi-agent-career-assistant；业务逻辑与安全约束原样保留。

---

## 0. 结论先行

**可行**，且比预想的工作量小 —— 因为 pi-career-skills 的**全部业务层都是纯 Python、
零框架依赖**。真正要重写的只有 3 个文件（tool_adapter / factory / controller+hooks），
其余全部以库的形式复用。重写顺序按成本从低到高：

| 工作 | 成本 | 说明 |
|---|---|---|
| 钩子映射 | 低 | 5 个 pi-py hook → deepagents middleware 钩子，见 §3 |
| 16 工具 → BaseTool | 中 | 新写 `tools_adapter.py`，handler 原样复用 |
| harness → 中间件 | 中 | evidence/budget/stall/completion 各写一个中间件 |
| controller 编排 | 中 | `CareerRunController` 改用 `create_deep_agent` 驱动 |
| 评测/审计层 | 中 | 25 题 runner 重写，审计断言复用 |

---

## 1. 可移植性盘点（实测源码 import 结论）

### 1.1 直接复用（零 pi_agent_core 依赖，`grep` 已验证）

| pi-career-skills 模块 | 内容 | 复用方式 |
|---|---|---|
| `contracts.py` | ToolObservation / Artifact / RunEvent | `import` 原样 |
| `context.py` | ToolContext | `import` 原样 |
| `errors.py` | 稳定错误码 + 脱敏 | `import` 原样 |
| `registry.py` | 16 个 ToolDefinition + `CareerToolRegistry` + `TOOL_CATALOG_BY_SKILL` | `import` 原样 |
| `business/*` | 4 技能纯函数 handler | `import` 原样 |
| `network/*` | SSRF 安全抓取/浏览/搜索/微信 | `import` 原样 |
| `runtime/evidence.py` | EvidenceStore（证据提升/质量门） | `import` 原样 |
| `runtime/budgets.py` | BudgetLimits / BudgetConsumed / BudgetTracker | `import` 原样 |
| `runtime/completion.py` | 完成门四闸 + matching_fallback | `import` 原样 |
| `runtime/state.py` `events.py` `recovery.py` | run 状态 / 事件总线 / 恢复策略 | `import` 原样 |
| `agents/capabilities.py` | CAPABILITY_REGISTRY（技能描述/预算/模型键） | `import` 原样 |
| `agents/prompts.py` | SUPERVISOR_PROMPT + 4 技能 prompt 常量 | `import` 原样 |
| `agents/contracts.py` | AgentTask / DelegationOutcome / 六态 | `import` 原样 |
| `resources/archived_skills/*` | 4 份 SKILL.md（SkillsMiddleware 的数据源） | `FilesystemBackend` 指向 |
| `evaluation/seed_urls.py` `profile_facts.py` | 25 题种子 URL / 简历事实 | `import` 原样 |

### 1.2 必须重写（框架绑定）

| 文件 | 绑定点 | deepagents 替代 |
|---|---|---|
| `tool_adapter.py` | `AgentTool` / `AgentToolResult` 协议 | → `tools_adapter.py`：包装成 `BaseTool` |
| `agents/delegation_tools.py` | `AgentTool`（delegate-*） | → `SubAgentMiddleware` 的 `task` 工具 |
| `agents/factory.py` | `Agent(AgentOptions(...))` | → `create_deep_agent(...)` + `subagents=` |
| `runtime/agent_hooks.py` | 5 个 hook 回调 | → 4 个自定义中间件（§3） |
| `runtime/controller.py` | 驱动 pi Agent 循环 | → `CareerRunController` 驱动编译后的图 |

---

## 2. 目标结构

```
deepagents_packages/
├── MIGRATION.md
├── pyproject.toml                  # deepagents / langchain-openai / pi-py-career-skills
├── src/deepagents_skills/
│   ├── models.py                   # DeepSeek / faux BaseChatModel（对应 model_factory.py）
│   ├── tools_adapter.py            # 16 ToolDefinition → langchain BaseTool（§4）
│   ├── skills.py                   # 4 个 SubAgent spec + supervisor prompt（§5）
│   ├── run_state.py                # RunState：共享 EvidenceStore/Budget/EventBus
│   ├── context_bridge.py           # RuntimeContextProjection 实时投影桥
│   ├── chain.py                    # 链式 artifact seed 投影
│   ├── middleware/
│   │   ├── sequential.py           # 自定义：串行化工具执行（§6.1）
│   │   ├── evidence.py             # 自定义：证据提升 + stall 检测 + steer（§6.2）
│   │   ├── budget.py               # 自定义：墙钟 + 预算计费（§6.3）
│   │   ├── completion.py           # 自定义：完成门终止（§6.4）
│   │   └── harness.py              # build_middleware_stack()：官方 + 自定义组合（§7）
│   ├── controller.py               # RunRequest → RunResult（§8）
│   └── eval_25_deepagents.py       # 25 题 runner，复用 pi schema/audit（§9）
├── main_deepagents.py              # 根目录，镜像 main.py（§10）
└── tests/                          # 单元 + 冒烟
```

---

## 3. 钩子映射表（pi-py hook → deepagents middleware）

| pi-py hook（agent_hooks.py） | 业务语义 | deepagents 中间件 | 位置 |
|---|---|---|---|
| `stream_fn` | 计费（consume_turn / consume_model_request）+ halt 记录 | `BudgetMiddleware.wrap_model_call` | 自定义 |
| `get_api_key` | provider → key | 无（`ChatOpenAI(api_key=...)` 配在模型对象上）；`_model_for` → 每 SubAgent 独立 `model` | — |
| `before_tool_call` | 重复调用阻止 + 预算准入（block/terminate） | `ToolCallLimitMiddleware`（次数上限）+ `BudgetMiddleware.wrap_tool_call`（短路返回错误 ToolMessage） | 官方+自定义 |
| `after_tool_call` | 证据提升 / 错误码分类 / stall 信号 / soft-stall steer / 交付终止 | `EvidenceMiddleware.wrap_tool_call`（post-handler 检查 ToolMessage）+ `CompletionMiddleware.after_model`（Command jump_to end） | 自定义 |
| `should_stop_after_turn` | token + 墙钟超时停止 | `ModelCallLimitMiddleware`（模型调用数）+ `BudgetMiddleware.after_model`（`@hook_config(can_jump_to=["end"])` 返回 `{"jump_to":"end"}`） | 官方+自定义 |

**结构层等价**：
- 薄 supervisor → `create_deep_agent(subagents=[...])` 只给 supervisor 加 `task` 工具（等价于 4 个 `delegate-*`）
- 技能工具白名单 → 每个 SubAgent spec 的 `tools=[...]`（天然隔离；`read-skill-reference` 由 handler 内再验 `allowed_skills`）
- 每个 task 委托在 deepagents 官方 `SubAgentMiddleware` 调用期间切换到 pi 的 capability child budget，返回后恢复 supervisor tracker；model 按 `CapabilityDefinition.model_key` 路由
- 委托契约 → SubAgent 用 `response_format` 返回结构化 `DelegationOutcome`
- 证据库黑板书架 → `RunState`（经 `ToolRuntime` 的 configurable context 或共享对象注入）
- 每 agent 挂 hook → SubAgent spec 支持 `middleware` 字段（`create_sub_agent` 会透传，已读源码确认）

---

## 4. tools_adapter.py：16 工具 → BaseTool

```python
class CareerLangchainTool(BaseTool):
    """包装一个 ToolDefinition，handler/校验/脱敏全部复用 registry.invoke。"""
    name: str
    description: str
    args_schema: type[BaseModel]
    definition: ToolDefinition
    context: ToolContext           # 绑定 skill_name 的 run 上下文

    def _run(self, **kwargs) -> str:
        obs = registry.invoke(self.definition.name, self.context, kwargs)
        return json.dumps(obs.output if obs.status == "succeeded"
                          else {"status": obs.status, "error_code": obs.error_code,
                                "error_message": obs.error_message}, ensure_ascii=False)
```

要点：
- 模型可见输出与 pi 版**逐字节一致**（`ToolObservation` 序列化），skill prompt 无需改动
- `bound_content` / 稳定错误码 / 脱敏 → `registry.invoke` 内部已有（`invoke_tool_sync`）
- 模型边界的 skill isolation 复用 pi adapter 的 `_is_skill_allowed`；trusted-kernel 调用默认仍不启用隔离
- 共享工具 `read-skill-reference`：每个技能目录都含它，handler 内校验 `allowed_skills`
- 16 工具全部由 `build_career_tool_registry()` 驱动，`TOOL_CATALOG_BY_SKILL` 决定每个 SubAgent 的工具列表

---

## 5. skills.py：4 个 SubAgent spec

```python
SUBAGENTS = [
    {"name": "job-discovery", "description": CAPABILITY_REGISTRY["job-discovery"].description,
     "system_prompt": JOB_DISCOVERY_PROMPT + load_archived_skill_prompt(...),
     "model": chat_openai_deepseek(), "tools": make_tools("job-discovery", ctx),
     "middleware": [...子 agent 中间件...],
     "response_format": DelegationOutcome},   # 类型化委托契约
    ...  # job-matching / resume-tailoring / career-planning
]
```

- 技能描述取自 `CAPABILITY_REGISTRY`（渐进披露，supervisor prompt 只给名字+描述）
- system_prompt = 三层合成（归档 SKILL.md + 运行时 prompt），与 pi 版 prompts.py 逻辑一致
- supervisor prompt 直接用 `SUPERVISOR_PROMPT` 常量

---

## 6. 自定义中间件（官方没有的 harness 语义）

### 6.1 SequentialToolMiddleware — 串行化工具执行
langgraph 默认**同轮并行**执行全部工具调用，而 pi harness 假设"同 run 永不并发执行业务工具"
（tool_adapter.py:16 注释原文），stall/证据/预算/请求治理都按单工具顺序写。
实现：`wrap_tool_call` 内 `asyncio.Lock` 串行化。

### 6.2 EvidenceMiddleware — 证据提升 + stall 检测 + soft-stall steer
- post-handler：解析 ToolMessage → `ToolObservation` → `store.add_observation(...)`（复用 EvidenceStore 质量门）
- 记录"最后一轮新证据时间"→ 连续 N 轮无新证据时返回 `Command(update={"messages": [HumanMessage(steer)]})`
  （对应 pi 版 `_SOFT_STALL_WRAP_UP` 文案）

### 6.3 BudgetMiddleware — 墙钟 + 预算计费
- `wrap_model_call`：`tracker.consume_turn()` / `consume_model_request()`（复用 BudgetTracker 硬顶）
- `after_model` + `@hook_config(can_jump_to=["end"])`：墙钟超时 → `{"jump_to":"end"}`
- 模型调用次数上限交给官方 `ModelCallLimitMiddleware`

### 6.4 CompletionMiddleware — 完成门终止
- supervisor 的 `after_model`：调 `RunCompletionPolicy.evaluate(...)`，完成 → `{"jump_to":"end"}`
  （对应 pi 版 `_deliverable_ready` 终止语义；`completion.py` 四闸原样复用）

---

## 7. 官方中间件选用表（18 个生态中本方案用 10 个）

| 中间件 | 来源 | 用途 | 位置 |
|---|---|---|---|
| `SubAgentMiddleware` | deepagents | 4 skill 子 agent 委托（结构核心） | supervisor |
| `SkillsMiddleware` | deepagents | 技能渐进披露（archived_skills/ → backend） | supervisor |
| `ModelRetryMiddleware` | langchain | 模型调用瞬时失败重试（有界自动恢复的一部分） | 全体 |
| `ToolRetryMiddleware` | langchain | 网络工具瞬时失败重试（指数退避） | 全体 |
| `ToolCallLimitMiddleware` | langchain | 单轮工具调用次数上限 | 全体 |
| `ModelCallLimitMiddleware` | langchain | 模型调用次数上限（预算的一部分） | 全体 |
| `ToolErrorMiddleware` | langchain | 工具错误规范化（错误码稳定） | 全体 |
| `SummarizationMiddleware` | langchain | 长上下文压缩（预算防护） | 全体 |
| `MemoryMiddleware` | deepagents | 跨轮记忆（可选，评测默认关） | 可选 |
| `PIIMiddleware` | langchain | 简历 PII 保护（resume-tailoring 场景） | 可选 |

**不选用**（与本 harness 定位冲突）：`HumanInTheLoopMiddleware`（评测要全自动）、
`FilesystemMiddleware`/`PermissionsMiddleware`/`ShellToolMiddleware`（无 shell/文件工具）、
`RubricMiddleware`（评测审计已有确定性闸门）、`ModelFallbackMiddleware`/`LLMToolSelectorMiddleware`
`LLMToolEmulator`/`ContextEditingMiddleware`/`ProviderToolSearchMiddleware`（非本 harness 需求）。

**中间件顺序**（`harness.py` 内组装，外层包裹内层）：
```
[SequentialTool, Budget, Evidence, Completion(仅 supervisor),
 ModelRetry, ToolRetry, ToolCallLimit, ModelCallLimit, ToolError, Summarization]
```

---

## 8. controller.py：编排

```python
class CareerRunController:
    async def run(self, request: RunRequest) -> RunResult:
        state = RunState(request)                     # 复用 EvidenceStore/BudgetTracker/EventBus
        graph = create_deep_agent(
            model=chat_openai_deepseek(),
            system_prompt=SUPERVISOR_PROMPT,
            subagents=SUBAGENTS,
            middleware=build_middleware_stack(state),
            backend=StateBackend(),
        )
        await graph.ainvoke({"messages": [HumanMessage(request.task)]},
                            config={"configurable": {"run_state": state}})
        return RunResult(...)                         # 复用 completion/state/events 构造终态
```

- `RunResult` 字段（run_id/status/summary/error_code/attempt_count/completed_skills/refs/artifacts/events/budget）与 pi 版一致，评测记录 schema 兼容
- faux 冒烟：`FakeMessagesListChatModel` 或最小 FakeChatModel，免 API key

---

## 9. 25 题评测（deepagents 版）

- 数据源不变：`eval_results/questions/normalized/<qid>.json` + `manifest_25_ids.json`
- `eval_25_deepagents.py`：单进程顺序跑，链式环节通过 `deepagents_skills.chain` 传递 seed artifacts
- 每个输出直接按 pi 的 `pi_eval_record_v1` schema 校验；audit/schema 复用 `pi_career_skills.evaluation`，不复制整套评估层

---

## 10. main_deepagents.py

镜像 `main.py`：`run_career_task(question, resume_url, ...)` → 复用 `profile_facts` 提取简历事实、
`resolve_seed_urls` 注入种子、跑 `CareerRunController`、打印终态 + 工具日志（`temp/results/tool_calls_deepagents.jsonl`）。

---

## 11. 验证

1. 单元：tools_adapter、context projection、middleware termination、capability routing、chain seed（8 项 deepagents 回归）
2. 冒烟：`faux` 模型跑通 supervisor → task → 子 agent 全链路
3. 25 题：`eval_25_deepagents.py` 单进程跑完，与 pi 版 20/25 基线对比
4. 安全回归：SSRF 守卫 / 同意闸门 / 无登录态 —— network 层原样复用，自动继承
