# 站点适配器（当前项目）

适配器入口是 `pi_career_skills.network.adapters`。它是关闭优先的、只读的公开 API 通道，不是绕过登录或反爬的通道。

## 使用条件

- 运行时必须显式开启 `enable_public_api_adapters(True)`。
- URL 必须命中经过审核的 adapter host allowlist。
- 适配器异常、空结果或 schema 不匹配都降级为稳定失败；不能伪造页面证据。
- 未命中适配器时自动回到 `requests -> Playwright` 页面抓取链。

当前包默认不依赖原项目 `skill/job-discovery/scripts/adapters` 目录；默认路径是普通公开页面抓取。新增 adapter 应放在包内受审查模块，并补充 allowlist、SSRF、空结果和错误降级测试，不要从任意文件路径动态导入。
