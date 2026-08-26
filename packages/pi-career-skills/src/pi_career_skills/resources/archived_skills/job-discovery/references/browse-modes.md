# 公开页面抓取与浏览器交互模式

本项目的页面抓取与浏览器交互全部由**注册工具**完成，不存在 `scripts/browse.py
--mode ...` CLI。相关工具：

- `fetch-public-job-page`：抓取一个公开 URL（requests 快速路径 + Playwright 渲染兜底）。
- `fetch-public-job-pages`：一次抓取 1～10 个 URL，返回 `pages` 与逐 URL `failures`，
  并对同站岗位详情链接做有界展开（含 URL 分页兄弟页，见下文"URL 分页"）。
- `search-public-job-pages`：在公开搜索源中发现招聘详情 URL；**不是**招聘站内搜索框自动化。
- `classify-job-url`：用低预算探针判定 `wechat/adapter/static/spa/blocked`。
- `browse-public-job-page`：在无头浏览器中打开一个公开职位页并交互（四种 mode，见下）。
- `search-job-site`：驱动站点**自带**的站内搜索框，返回搜索后的页面作为证据。

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

`fetch-public-job-pages`（`batch_fetch.py`）会对符合条件的同站岗位详情链接做**有界
展开**，并检测**服务器渲染的 URL 分页列表**（见下文），但不会无限翻页。

## browse-public-job-page：浏览器交互（P1/P2）

当 `fetch-public-job-page` 返回 `js_shell`，或列表页是 JS 渲染的 SPA 时，用
`browse-public-job-page` 在无头 Chromium 中重渲染并交互。参数：

- `url`：公开职位页 URL（必须通过公开 URL 校验，禁止登录/内网地址）。
- `mode`：四种自动化深度：
  - `render`：仅渲染 + 稳定等待（JS SPA 的正文重渲染）。**默认**，行为与
    `fetch-public-job-page` 的渲染兜底一致。
  - `load-all`：滚动页面触发懒加载（"加载更多"/无限滚动），把整个列表加载出来。
  - `paginate`：检测 URL 分页模式（`?page=N` / `?offset=N&limit=M` / `/page/N/`），
    依次跳转第 2..N 页收集列表。
  - `interact`：点击卡片展开详情（适用于点击卡片后右侧/弹层显示 JD 的站点），
    最多点 `max_cards` 张，受 120 秒预算约束。
- `pages`（1~5，默认 3）：`paginate` 模式最多收集的页数。
- `max_cards`（1~12，默认 5）：`interact` 模式最多点击的卡片数。
- `wait_ms`（200~10000，默认 1500）：稳定文本轮询的等待步长。

**输出 = 完整证据页 + 引导信号**：

- 证据字段与 `fetch-public-job-page` 完全同契约（`artifact_id` / `source_url` /
  `content_hash` / `visible_text` / `quality` / `http_status` / `detail_links`），
  所以返回的页面可以直接进 `extract-observed-job-details`。
- 引导信号（agent 据此决定下一步）：
  - `strategy` / `strategy_detail`：实际执行的策略
    （`render` / `load_all` / `url_jump` / `interact` / `single`）与说明。
  - `cards_visible`：页面上可见的岗位卡片数（JS 去重计数，上限 200）。
  - `estimated_total_items`：从总数指示文本估计的全站/列表总量（可能为 null）。
  - `pagination_pattern`：检测到的 URL 分页模式（`query` / `offset` / `path`），
    无分页时为 null。
  - `pages_collected`：`paginate` 模式实际收集的页数。
  - `warning`：非致命提示（如列表为空、未找到分页、点击预算用尽）。

### 使用决策

1. `js_shell` 页面 → `mode=render` 重渲染；仍无正文则如实报告。
2. SPA 列表需要滚动加载 → `mode=load-all`。
3. URL 带分页模式（`?page=` / `?offset=` / `/page/`）→ `mode=paginate`
   （对服务器渲染列表，`fetch-public-job-pages` 已自动收集兄弟页，无需再用本模式）。
4. 卡片点击展开详情的站点 → `mode=interact`。
5. `warning` 或引导信号说明列表为空/总量为 0 时，停止该站，不要空转。

## search-job-site：站内搜索

当站点自带搜索框、需要在**该站内**找特定关键词岗位时使用，与外部搜索引擎工具
`search-public-job-pages` 严格区分。

- 参数：`url`（列表/搜索页 URL）、`query`（岗位关键词，1~80 字符）、`max_cards`。
- 输出：**搜索后页面作为完整证据**（可直接提取），加搜索诊断：
  - `search_ok` / `search_detail`：是否找到搜索框并执行搜索，及失败原因。
  - `pre_search_card_count` / `post_search_card_count`：搜索前后可见卡片数。
  - `result_indicator`：从"共 N 个结果"等指示文本读取的结果数。
  - `warning`：**post 与 pre 卡片数相同**时提示"疑似前端假过滤"——搜索框可能只是
    客户端过滤，完整列表仍在 DOM 中，结果可能不完整。

## URL 分页（fetch-public-job-pages 内建）

`fetch-public-job-pages` 的 `batch_fetch.py` 对**服务器渲染**的 URL 分页列表自动
收集第 2..N 页（`_MAX_PAGINATION_PAGES=3`）并合并各页详情链接做有界展开。触发条件：
种子 URL 本身带可检测的分页模式，且第 1 页是列表页（正文前部不含 JD 区块标记）。
同页重复 / 重定向回第 1 页 / 抓取失败都会提前停止。**详情页永远不会被分页**。

## 安全边界（必须遵守）

- 只访问公开 URL；所有子资源经公开 URL 路由守卫放行，禁止登录态、内网地址、SSRF。
- 不做登录、不做验证码/滑块/扫码绕过。出现验证码、登录墙、403/429、反爬挑战时，
  如实报告 `needs_manual_review`，保留失败原因。
- 弹窗/同意框只会点击与"同意/接受/知道了"等**白名单**文本匹配的按钮；命中
  `验证码 / 登录 / 扫码 / 请在微信客户端打开` 等黑名单关键词时直接中止，绝不点击。
- 渲染从不加载任何登录 profile。
- 失败保留原因：`js_shell`、`empty`、验证码、登录墙、403/429 不得当作完整 JD，
  也不能自动破解验证或绕过访问控制。

## 证据与失败处理

抓取成功后必须把返回的页面作为工具观察结果交给 EvidenceStore，再用
`extract-observed-job-details` 或 `extract-observed-job-details-batch` 规范化。
不要读取 `output/evidence/*.txt`，也不要把正文复制进下游 goal；跨 agent 只传
`artifact_id`、`source_url`、`content_hash`。

去重（`deduplicate-observed-jobs`）额外识别：**content-hash 相同**的页面（同一页面
被多次抓取）、**同公司同名**的职位（公司不同名不合并）、以及**echo 片段**——搜索
结果/列表卡片这类短片段与同一职位更丰富的完整 JD 同时存在时，片段被折叠到完整 JD
（reason `echo_of`），优先保留更完整的页面而不是先出现的页面。
