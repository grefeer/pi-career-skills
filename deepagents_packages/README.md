# deepagents-skills

Pi-career-skills 的 deepagents + middleware 移植版（薄 supervisor + 4 个 skill 子 agent
结构不变），迁移方案见 `MIGRATION.md`。

- `deepagents_skills.skills` — supervisor graph + 4 技能 SubAgent
- `deepagents_skills.middleware.harness` — 4 个自定义中间件 + 官方中间件栈
- `deepagents_skills.controller` — 运行控制器（attempts / budget / evidence / recovery）
- `eval_25_deepagents.py` — 25 题回归测试脚本
- `main_deepagents.py` — 与 `main.py` 对齐的 CLI 入口
