# pi-career-skills

Pi-py 移植的求职多 Agent 评测运行时：四个技能（job-discovery / job-matching /
resume-tailoring / career-planning）、13 个确定性工具、以及 run 级 harness（证据库、完成门、预算、
stall、有界自动恢复）。

> 状态：评测/对齐运行时（内存态 run 级状态），不是生产后端。迁移实施计划见源项目
> `docs/pi-py-agent迁移计划.md`；13 工具契约快照见本包
> `tests/fixtures/pi_contract_snapshot.json`。

## 结构

```text
src/pi_career_skills/
├── contracts.py        # Observation / Result / Budget / Event DTO
├── context.py          # ToolContext、公开/私有投影
├── errors.py           # 稳定错误码与脱敏
├── registry.py         # 13 ToolDefinition（唯一真值）
├── tool_adapter.py     # AgentTool 适配、双向校验、to_thread
├── model_factory.py    # DeepSeek / faux Model
├── business/           # 从源项目移植的业务逻辑（纯函数）
├── runtime/            # state / events / evidence / budgets / completion / recovery / controller
├── agents/             # prompts / delegation tools / agent factory
├── network/            # Playwright worker
├── resources/          # data/*.json + archived SKILL.md
└── evaluation/         # schema / chain / audit / runner / compare
```

## 开发

```powershell
uv sync
uv run ruff check packages/pi-career-skills
uv run pytest packages/pi-career-skills/tests -q -m "not integration"
```
