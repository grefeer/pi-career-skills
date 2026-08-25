# job-discovery 当前数据契约

## 页面证据

`fetch-public-job-page(s)` 返回的每个成功页面至少包含：

| 字段 | 含义 |
|---|---|
| `artifact_id` | EvidenceStore 中的稳定引用 |
| `source_url` | 用户候选或搜索发现的公开 URL |
| `visible_text` | 有界页面正文；列表壳可能被截短 |
| `content_hash` | 页面正文 SHA-256 绑定 |
| `quality` | `jd_complete` / `list_only` / `js_shell` / `empty` |
| `detail_links` | 同站、有限、经 URL 校验的详情链接 |

失败项位于 `failures`，包含 `source_url`、稳定 `error_code` 和可选 HTTP 信息。

## 结构化 JD

`extract-observed-job-details(-batch)` 输出：

`source_artifact_id`、`source_url`、`content_hash`、`source_quality` 和 `candidates`。
每个 candidate 包含 `title`、`company_name`、`locations`、`responsibilities`、
`requirements`、`recruitment_types`、`apply_url`、`deadline_text`、`confidence`、
`evidence_refs` 与 `normalization_warnings`。

`evidence_refs` 必须引用原始页面的 `content_hash` 或受信来源字段；不能用模型猜测的
URL、正文或薪资填充证据。

## 质量规则

- `list_only`、`js_shell`、`empty` 不能直接生成完整 JD。
- 职责与要求无法在原文分开时，写入 `normalization_warnings`，不要静默推断。
- 缺失字段写成未确认，不要把缺失当作不满足。
- 下游只传 `artifact_id`、`source_url`、`content_hash`，正文由工具从 EvidenceStore 读取。
