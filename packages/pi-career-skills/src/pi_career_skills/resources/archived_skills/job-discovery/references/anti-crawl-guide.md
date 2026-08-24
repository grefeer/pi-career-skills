# 登录与反爬指南（L2ac）

> 目标站命中 `anti_crawl/site_registry.py` 档案、或页面出现登录墙/403/滑块时使用。
> 与 `references/site-adapters.md` 配套。仅个人账号、个人求职用途。

## 何时走反爬层

Phase 2 分类判定目标站后：

```
┌─ 目标站 ── 在 site_registry 有档案？ ──否──▶ 现有 browse.py 路径（不变）
│                        │是
│                        ▼
│              check_login.py 健康检查
│                        │
│        ┌───────────────┼────────────────┐
│        ▼               ▼                ▼
│    未登录          被风控             就绪
│    login.py       记录 blocked        crawl.py
│    （人工）        等待/换策略          输出同构 JSON
│        │               │                │
│        └────复检────────┘                ▼
│                              Phase 4-6 现有提取/校验/去重/持久化（不改）
```

## 命令速查

```bash
# 1. 健康检查（先查再爬）
python scripts/check_login.py                    # 全站
python scripts/check_login.py --site liepin      # 单站

# 2. 交互式登录（人工扫码/短信/滑块，一次性，profile 持久化）
python scripts/login.py --site liepin

# 3. 登录态爬取
python scripts/crawl.py --site liepin --keyword "AI" --max-pages 3
python scripts/crawl.py --site moka --url "https://xxx.mokahr.com/social-recruitment/jobs" --mode detail

# 4. 本地自检（改完 anti_crawl/ 后必跑）
python scripts/anti_crawl_selftest.py && python -m pytest anti_crawl/tests/
```

## 状态语义（crawl.py stdout JSON）

| status | 含义 | 下一步 |
|---|---|---|
| `ok` | 正常抓取，evidence 已落盘 | 走 Phase 4-6 |
| `blocked:slider` / `blocked:captcha` | 人工处理超时（5 分钟） | 稍后重试；不要在风控窗口内猛刷 |
| `blocked:js_challenge` | 加速乐/瑞数类 5s 未自动通过 | 记录 blocked，等待，换时段再试 |
| `blocked:login_wall` | 登录墙（或登录态过期） | `check_login.py` → `login.py` 重新登录 |
| `blocked:rate_limited` | 403/429 退避 3 次未恢复 | 停止当日该站抓取，明天再试 |
| `needs_manual_review` | 需人工介入（未登录/日上限/站改版） | 按 hint 处理 |
| `error` | 参数/环境错误 | 读 error 字段 |

## 错误处理矩阵（与规格 §7 一致）

| 情境 | 处理 |
|---|---|
| 403 / 429 | pacing 指数退避（30/60/120s，3 次）→ `blocked:rate_limited` |
| 滑块/验证码 | 有头窗口暂停等人工，5s 轮询，5 分钟超时（**绝不自动破解**） |
| 登录墙 | 提示 `login.py`；已登录仍出现 → 登录态过期，重新登录 |
| JS challenge 未通过 | 5s 后 `blocked:js_challenge`，如实报告 |
| 登录态过期 | `check_login.py` 检出 → 提示重新登录 |
| 签名参数站 | 不在范围；标注"需 Firefox-Reverse 逆向"（见下） |
| 站改版/选择器失效 | 记录 `blocked:site_changed` + 页面截图，更新 site_registry 档案 |

## 合规边界（红线，代码与文档同约束）

- 仅个人账号、个人求职用途；数据仅个人聚合使用。
- **不破解验证码、不自动化短信、不注入 JS 绕过站点风控逻辑**；stealth 仅使
  浏览器表现为正常用户浏览器（隐藏自动化痕迹 ≠ 绕过安全机制）。
- 礼貌频率为默认纪律：页面间 `random(2,5)s`、单页并发 1、单站每日上限 500 页。
- 尊重各站 robots.txt 与条款；因高频或滥用导致的账号风险由使用者承担。
- **签名参数站**（需逆向请求签名算法，如加速乐 token/自研 sign）：不在本 skill
  范围——那是 Firefox-Reverse 逆向工具链的领域，由 `docs/jsvmp-reverse-workflow.md`
  覆盖。遇到此类站如实标注，不要尝试在爬虫层绕过。

## 与 browse.py 的关系

- `crawl.py` 输出 JSON 与 `browse.py` 同构（status/url/title/content_hash/text_path/
  screenshot_path + 扩展字段），Phase 4-6（提取/校验/去重/持久化）**一行不改**。
- 证据文件同一套 sha256 命名，落在同一 `output/evidence/`。
- 缓存键带 `ac::<site>::` 前缀，与 browse.py 的裸 URL 键隔离，互不污染。
- 反爬层**不替代** browse.py：静态站/公开站仍走原路径；反爬层只接管
  登录墙/风控/需登录的站。

## 频率与节奏（行为风控站重点）

- 猎聘类行为风控站：登录后第 1 天只爬 1-2 页"热热身"，逐日缓慢加量。
- 翻页间隔用档案 `base_interval_s`，不要全局改小；被 429 后主动停 30 分钟。
- 同一站点同一时刻只跑一个 crawl.py（同一 user_data_dir 不支持并发，会锁 profile）。
