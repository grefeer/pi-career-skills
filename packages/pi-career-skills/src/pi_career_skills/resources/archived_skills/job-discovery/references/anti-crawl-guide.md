# 公开招聘页面的反爬与人工兜底

当前项目的实现入口是 `network.page_fetch`、`network.batch_fetch`、`network.request_governor` 和 `network.url_guard`，不是原项目的 `check_login.py` / `login.py` / `crawl.py` CLI。

## 处理顺序

1. `url_guard` 只允许公开 HTTP(S) URL，并拒绝用户信息、私网和云元数据地址。
2. `request_governor` 对同域请求做缓存、去重、节流和失败记忆。
3. 先使用有限的 `requests` 路径；只有允许的错误才启用 Playwright fallback。
4. 页面结果必须标记 `jd_complete`、`list_only`、`js_shell` 或 `empty`。

## 稳定失败语义

验证码、登录墙、403/429、anti-bot challenge、域名暂时阻断和 Playwright 不可用都必须进入 `failures` 或 `ToolObservation.error_code`。可接受的动作是换公开 URL、展开少量详情链接、使用公开搜索，或请求用户人工提供可访问链接。

## 合规边界

不破解验证码、不注入脚本绕过安全控制、不自动提交登录、不逆向签名参数。Playwright 只用于公开页面渲染，并继续执行 SSRF/public-host 检查。不要高频重试，也不要把失败页包装成完整 JD。
