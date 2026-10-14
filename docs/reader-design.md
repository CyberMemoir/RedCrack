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
- `evidence.py`：独立保存网页响应、详情状态及可见卡片；保留原文、读取时点、指纹和字段位置。访问链接与稳定公开链接分开。
- `xhs.py`：输入验证、批处理、实时 checkpoint、结构化执行结果与退出码。

搜索卡片只代表摘要。批量下载每次都重新读取详情，防止把搜索封面当作完整图片列表，或直接重用已经失效的媒体链接。

## 全自动访问控制与提醒停止

`runtime.py` 负责profile独占文件锁、持久预算和不含访问令牌的动作日志。
每段最多3次搜索/12篇详情/10分钟，达到任一条件休息20分钟；正常休息自动接续。
导航、翻页及媒体请求共用最小间隔；失败尝试同样消耗预算。休息不超过30秒一次等待，
间隔期间仍检查工具暂停。没有伪造鼠标轨迹、浏览器指纹或隐藏自动化。

`cache.py` 将成功搜索卡片和完整详情分开存放；以精确note ID复用详情，
以关键词和参数摘要复用搜索，`--refresh` 才重新取得内容。缓存原观察时间不改为今天。
CLI支持有限关键词文件；跨关键词按ID去重。每次搜索最多翻页2次，无新增结果结束。
完成搜索缓存可接续，未完成搜索的输出只是checkpoint，不能冒充完成缓存。

浏览器按需启动，进入 `async with XHSReader(...)` 只取得profile锁；
直接访问 `reader.page` 的调用方须先 `await reader.start()`。常规search/get_note/login会自动启动。
媒体默认并发1、重试最多1次；访问限制立即传播，取消尚未完成媒体任务。
媒体仍校验现有文件指纹后跳过，批次JSON保留历史尝试，不把旧失败当最新结果。

`access.py` 将已识别的行为提醒/验证码/访问限制写入profile暂停记录。
每次启动和导航前检查记录，HTTP 403/412/429/461/471、响应错误文字以及
活动弹窗中的提醒会中止读取。笔记正文提及相同词语不会作为账号提醒。
识别依赖已知弹窗结构和词语，不保证覆盖平台所有新提醒。
`acknowledge-warning` 仅在本地记录用户说明并解除工具暂停；不访问平台、
不自动安排重试，也不声称解除账号限制。
发现提醒时保存profile内关键截图和动作历史，触发归因保持未知，尤其不能把
提醒前最后一项操作认定为原因。没有模拟平台机器人判定，也未以账号线上验证。

## 状态与失败

CLI `task.json` 是最近一次任务状态，每次任务另存 `tasks/<task_id>.json`。
返回登录、验证、网络等待超时或中断时保存终态；`results.json.complete` 只描述结果阶段。
详情和响应体超时是 `NetworkReadError`，不作为单篇普通失败继续下一篇；多关键词失败前的结果增量保留。
搜索翻页等待网页实际响应或新卡片，未收到时超时停止，不把短暂空白缓存成完整结果。
`Note.stats` 保留原显示值，未提供的字段为null；`canonical_url` 按稳定ID生成，
不能把另一ID的fallback访问令牌附到当前来源。正常读取时附证据，缓存命中不改旧观察时间。

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
