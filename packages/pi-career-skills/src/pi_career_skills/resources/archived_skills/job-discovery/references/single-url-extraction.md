# 单 URL 提取流程（当前项目）

## 1. 选择工具

- 用户给出一个 URL：调用 `fetch-public-job-page`。
- 用户给出多个 URL：调用 `fetch-public-job-pages`，每批最多 10 个。
- 没有 URL：先调用 `query-career-sheet-records`，没有匹配时才调用
  `search-public-job-pages`。
- 微信 URL：调用 `fetch-wechat-article`，不要把普通 HTML 抓取当作 OCR 结果。

## 2. 处理返回结果

只把 `quality=jd_complete` 的页面直接交给提取工具。若返回 `list_only`，从
`detail_links` 中选择有限数量的详情 URL，再调用 `fetch-public-job-pages`；
`js_shell` 和 `empty` 必须记录为不可用证据。

## 3. 规范化

调用 `extract-observed-job-details`（单页）或
`extract-observed-job-details-batch`（最多 10 个 `artifact_id`）。提取工具只接受
已持久化证据引用，不接受模型生成的正文、公司名或 URL。

## 4. 校验与去重

必要时调用 `validate-observed-candidates`，随后调用
`deduplicate-observed-jobs`。所有下游 agent 只接收持久化引用；不要把页面全文放进
delegation goal 或 `expected_output`。

## 5. 终止条件

收集到 3～6 个符合目标的可用 JD 后停止。连续两次没有新增可用证据时停止并报告：
已尝试的 URL、稳定错误码和需要用户提供的公开链接。禁止无限重试同一 URL。
