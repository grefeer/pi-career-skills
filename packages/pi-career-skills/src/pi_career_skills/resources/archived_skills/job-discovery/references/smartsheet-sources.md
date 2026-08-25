# 腾讯文档招聘台账来源

当前项目通过 `query-career-sheet-records` 调用 `pi_career_skills.network.career_sheets`。它使用本机 `mcporter` 的 `tencent-docs` bridge；不是直接在 agent 中调用 `smartsheet.list_tables`。

## 查询规则

- 输入是企业、岗位、地点关键词和近 N 天窗口。
- 返回记录包含公司、投递 URL、更新时间和可选内推元数据。
- 每个 sheet 扫描有界，输出记录有界；不要把整张表复制进 prompt。
- 有匹配记录时优先使用台账 URL，再抓取公开页面核实 JD。

## 失败与备用源

`sheet_rate_limited` 或 `sheet_call_failed` 时，不要在本轮重复调用台账；切换到 `search-public-job-pages`。腾讯文档 token 通过运行环境配置，不能写入 goal、日志或 reference。台账字段是先验元数据，不能替代公开页面正文证据。
