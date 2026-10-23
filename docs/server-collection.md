# 常驻服务器采集

`xhs.py discover/search/download --metadata-only` 是有界读取入口。生产调度使用 CyberMemoir 的独立 [采集服务](https://github.com/CyberMemoir/cyber-memoir/tree/codex/bilibili-text-collector-20261014/apps/collector)，不让 AI 会话循环执行命令。服务提供统一队列、窗口、租约、跨批去重、内容寻址归档、磁盘预算、状态指标和备份。

本工具负责登录 profile、访问暂停、浏览器锁、页面读取预算和原始证据。`task.json` 与 `results.json` 先写临时文件、flush/fsync 后原子替换；Linux 同步父目录。缓存、profile 预算和证据使用同一写入方式。访问暂停也采用持久写入，显式解除前先保存运维历史。

服务配置指定 `tool_dir`、本工具环境的 `python`、已有 `profile`。每个窗口使用独立输出目录并显式 `--refresh`；已有完整结果可以直接导入，未知的旧进程/运行中结果则停止，不盲目启动第二次。服务不覆盖现有间隔、分段休息或滚动上限；本工具向共享队列导出详情元数据，媒体可选由中央的独立配额下载。

推荐页只代表当前账号能看到的笔记，结束原因保存在 `discovery.json`。登录/验证立即停，扫码只能通过正常登录操作完成；工具修改不代表限制解除。无模型、无代理轮换、无签名绕过、无逐条内容审批，也不写正式梗库。

末页返回条数超过 `--limit` 时，输出仍是 `limit`，不能因为 `has_more=false` 就声称已完整导出入口。共享采集服务按版本 1 校验 `task.json` 和 `results.json`；成功任务必须有完整且计数一致的结果文件，空结果使用 `notes: []`，缺文件不等于空成功。

目前持久写入与离线浏览器回归有验证；真实 Linux 账号连续窗口、进程硬退出和告警到达还需服务器上线验收。

详情导出增加可选 `subtitle_tracks: [{url,language,label,kind}]`，只读取正常页面 `video track[src]`，不猜地址、不下载字幕、不调用 OCR/ASR。空列表表示未观察到轨道；不能据此声称视频没有字幕。旧缓存自动使用空列表，新字段保留在本地私有 JSON 中，媒体临时直链不得作为正式来源 URL。

共享服务 `collect_media=true`（默认关闭）只为明确视频类候选建立有界媒体任务：正常重新读取详情后，取首个视频直链和有限现成字幕，按平台 CDN HTTPS 白名单下载并绑定原 note_id/证据哈希。数量、字节、请求、超时、复用与恢复见中央 README。缺字幕、格式不支持或失效地址显式 partial；访问限制停止平台。此路径不自动生成 definition/usage_context 或衍生关系。

