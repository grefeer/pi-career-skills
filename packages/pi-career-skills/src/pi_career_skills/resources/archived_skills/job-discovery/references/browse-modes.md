# 当前项目的公开页面抓取模式

本项目没有 `scripts/browse.py --mode ...` CLI。页面抓取由注册工具调用：

- `fetch-public-job-page`：抓取一个公开 URL。
- `fetch-public-job-pages`：一次抓取 1～10 个 URL，并返回 `pages` 与逐 URL `failures`。
- `search-public-job-pages`：在公开搜索源中发现招聘详情 URL；它不是招聘站内搜索框自动化。
- `classify-job-url`：用低预算探针判定 `wechat/adapter/static/spa/blocked`。

## 单页与列表页

`fetch-public-job-page` 先走 `pi_career_skills.network.page_fetch` 的 SSRF 防护
和 `requests` 快速路径；命中动态页面或允许的 fallback 条件时，才使用
`pi_career_skills.network.playwright_worker`。输出包含：

`artifact_id`、`source_url`、`content_hash`、`visible_text`、`quality` 和有限的
`detail_links`。`quality` 只有以下值：

- `jd_complete`：正文足以进入 JD 提取。
- `list_only`：列表/卡片壳；应从 `detail_links` 选择详情页继续抓取。
- `js_shell`：动态页面没有捕获到可用正文。
- `empty`：空正文。

`fetch-public-job-pages` 在 `batch_fetch.py` 中会对符合条件的同站岗位详情链接
做有界展开；它不是原项目 `parallel-fetch` 的 URL 分页探测器，也不会无限翻页。

## 当前没有迁移的旧模式

以下名称只属于原项目脚本，不能在本 agent 中调用：

`interact`、`search-interact`、`click`、以及基于浏览器点击探测 URL 分页的
`parallel-fetch`。当前 Playwright fallback 只负责渲染、稳定等待和收集同站详情链接，
不负责通用的卡片点击或登录操作。

## 证据与失败处理

抓取成功后必须把返回的页面作为工具观察结果交给 EvidenceStore，再用
`extract-observed-job-details` 或 `extract-observed-job-details-batch` 规范化。
不要读取 `output/evidence/*.txt`，也不要把正文复制进下游 goal；跨 agent 只传
`artifact_id`、`source_url`、`content_hash`。

`js_shell`、`empty`、验证码、登录墙、403/429 和反爬挑战必须保留失败原因。
不能把列表壳或失败页当作完整 JD，也不能自动破解验证或绕过访问控制。
