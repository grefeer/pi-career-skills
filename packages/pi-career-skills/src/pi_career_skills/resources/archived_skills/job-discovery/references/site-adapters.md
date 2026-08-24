# 五站反爬档案（v1）

> 数据源：`anti_crawl/site_registry.py`（唯一事实源）。改档案必须同步改本文件并跑
> `python -m pytest anti_crawl/tests/test_site_registry.py` 校验。
> **档案是起点不是真理**：先观察后执行；站改版时按"实际观察优先"更新。

## 概览

| 站 | key | 防线类型 | 需登录 | 抓取模式 | 登录态信号 | 备注 |
|---|---|---|---|---|---|---|
| Moka | `moka` | weak（SPA 卡片抽屉） | 否 | search+detail | 无 | 无统一域名，`--url` 直传 |
| 牛客 | `nowcoder` | medium（部分内容登录墙） | 是 | search | 用户区元素 | 校招聚合 |
| 百度百聘 | `baidu` | medium（JS 挑战） | 否 | search | 无 | 入口实测定 |
| 58同城 | `58` | medium-strong（滑块+登录墙） | 是 | search | 右上角用户名 | 招聘列表反爬较强 |
| 猎聘 | `liepin` | strong（行为风控+频率） | 是 | search+detail | "我的求职"入口 | 登录后保持真实节奏 |

## 每站要点

### moka（mokahr.com 系）
- SPA 卡片抽屉：列表渲染后 `--mode detail` 逐卡片点击取 JD；详情链接标记 `#/job/`。
- 无统一搜索模板：每次 `--url` 直传目标列表页。
- 沿用 `references/site-catalog.md` 的 interact 思路（"查看更多职位"等加载按钮由
  crawl.py 的 load-more 识别兜底）。

### nowcoder（牛客）
- 校招信息聚合站；部分内容登录墙 → 先 `login.py --site nowcoder`。
- 登录信号当前为**首轮猜测**（用户区选择器/文本），Task 8 冒烟实测后校准。

### baidu（百度百聘）
- 可能走 302 / JS 挑战（"完成验证后即可继续访问"）；真实浏览器通常自动通过。
- 搜索模板 `https://zhaopin.baidu.com/s?wd={keyword}`（2026-08-13 实测）。旧域名
  `baijob.baidu.com` 已 DNS 失效（NXDOMAIN），浏览器只拿到 ERR_FAILED 错误页。
- 实测注意：zhaopin 搜索 URL 302 → `yiqifu.baidu.com/g/aqc/joblist` 且丢弃 wd 参数，
  渲染为北京泛职位列表（关键词过滤未生效）；无 JS 挑战，真实浏览器直接出内容。

### 58（58同城）
- 招聘列表反爬较强：登录墙 + 滑块。登录入口 passport.58.com。
- 搜索模板 `https://bj.58.com/job/?key={keyword}`（2026-08-13 实测；旧模板
  `jobs.58.com/search/` 已 404）。实测：curl 命中验证码页（"请输入验证码"），
  需真实浏览器人工过验证；模板写死北京，他城市需改模板。
- 登录信号（"退出"文本/用户区选择器）首轮猜测，冒烟校准。

### liepin（猎聘）
- 行为风控为主 + 频率限制：登录后第 1 天少量爬取热身，逐日加量。
- 详情链接标记 `/job/`；`--mode detail` 点卡片取 JD。
- 搜索模板 `https://www.liepin.com/zhaopin/?key={keyword}`；城市参数实测定。

## 新增站点步骤

1. 在 `site_registry.py` 的 `SITE_REGISTRY` 加条目（字段见该文件 docstring 与规格 §4.5）。
2. 跑 `python -m pytest anti_crawl/tests/test_site_registry.py` 通过校验。
3. `python scripts/login.py --site <key>`（需要登录的站）验证登录信号。
4. `python scripts/crawl.py --site <key> --keyword ... --max-pages 1` 冒烟，观察
   实际防线形态，校准 `login_signal` / `search_url_tpl` / `base_interval_s`。
5. 同步更新本文件概览表。

## 登录信号校准指引

`login_signal` 是 `{url_contains / selector / text}` 三元组，任一命中即"已登录"：
- 命中太宽（未登录也报已登录）→ 收窄选择器，或改 URL 特征。
- 命中太窄（登录了报未登录）→ 加备选选择器（逗号分隔）或补充 text。
- 改完立刻 `check_login.py --site <key>` 复核。
