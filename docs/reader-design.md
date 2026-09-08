# 搜索 / 下载模块设计

## 数据流

```text
xhs.py
  ├─ XHSReader.search → 网页搜索 → response 监听 / 可见卡片备用解析
  ├─ export_notes → results.json + results.csv
  └─ XHSReader.get_note → 网页详情 → feed 响应 / __INITIAL_STATE__
       └─ MediaDownloader → CDN 流式下载 → note.md + note.json + downloads.json
```

- `browser.py`：独立持久化浏览器、登录检测、搜索滚动、短链跳转、详情读取。仅监听网页自己发出的响应，不构造签名接口请求。
- `models.py`：snake_case / camelCase 归一化，访问令牌保留，媒体去重，视频清晰度选择。
- `downloader.py`：CDN 域名约束、逐次校验重定向、下载限流、有限重试、大小限制、文件头验证、SHA-256、原子替换与文件级续跑。
- `export.py`：原子 JSON / Markdown / CSV 导出、CSV 公式前缀保护。
- `xhs.py`：输入验证、批处理、实时 checkpoint、结构化执行结果与退出码。

搜索卡片只代表摘要。批量下载每次都重新读取详情，防止把搜索封面当作完整图片列表，或直接重用已经失效的媒体链接。

## 状态与失败

下载清单的素材状态为 `downloaded`、`skipped`、`failed`。笔记状态为 `running`、`complete`、`partial`。缺少视频直链时即使封面成功也标记 `partial`。

输出目录以笔记 ID 为键。完成一个素材即保存清单；被中断的 `.part` 临时文件在正常取消路径中清理。进程被强制终止遗留的隐藏 `.part` 可直接删除。再次运行会对清单内现有文件验证 SHA-256；中途未完成的单个文件重新下载。

支持并发下载同一篇笔记的多个不同素材，笔记详情顺序读取。不支持多个进程并发写同一个输出目录或打开同一 profile。

## 测试边界

1. 纯单元测试：分享链接、非法 URL、数据归一化、视频选择、去重、CSV、参数和退出码。
2. HTTP 集成测试：本地 aiohttp 服务器，实际流式传输、chunked 响应、媒体识别、大小上限、重定向、错误页、临时 503 重试、损坏文件重下、清单恢复。
3. 浏览器集成测试：真正启动 Chromium / Chrome，但用 Playwright 路由拦截请求并返回合成页面与 JSON；验证搜索翻页、限量、空结果、DOM 备用路径、登录/验证中止、详情双读取路径和短链跳转。

上述测试证明本地逻辑及浏览器链路可以运行，不等价于已登录小红书账号的线上全流程验收。登录态、地区网络与平台验证会影响线上结果。线上搜索/下载验收需要在用户自己的浏览器 profile 中扫码后执行 README 命令。

## 参考

- [Playwright 网络事件](https://playwright.dev/python/docs/network)
- [Playwright 持久化浏览器上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [aiohttp 客户端流式读取](https://docs.aiohttp.org/en/stable/client_quickstart.html#streaming-response-content)

依赖安装、浏览器启动或页面结构变化时，先区分运行环境错误、身份状态错误和解析错误，不应把未读取到数据一律解释为「搜索无结果」。
