# 常驻服务器采集

`xhs.py discover/search/download --metadata-only` 是有界读取入口。生产调度使用 CyberMemoir 的独立 [采集服务](https://github.com/CyberMemoir/cyber-memoir/tree/codex/bilibili-text-collector-20261014/apps/collector)，不让 AI 会话循环执行命令。服务提供统一队列、窗口、租约、跨批去重、内容寻址归档、磁盘预算、状态指标和备份。

本工具负责登录 profile、访问暂停、浏览器锁、页面读取预算和原始证据。`task.json` 与 `results.json` 先写临时文件、flush/fsync 后原子替换；Linux 同步父目录。缓存、profile 预算和证据使用同一写入方式。访问暂停也采用持久写入，显式解除前先保存运维历史。

服务配置指定 `tool_dir`、本工具环境的 `python`、已有 `profile`。每个窗口使用独立输出目录并显式 `--refresh`；已有完整结果可以直接导入，未知的旧进程/运行中结果则停止，不盲目启动第二次。服务不覆盖现有间隔、分段休息、滚动上限和下载设置，不下载媒体。

推荐页只代表当前账号能看到的笔记，结束原因保存在 `discovery.json`。登录/验证立即停，扫码只能通过正常登录操作完成；工具修改不代表限制解除。无模型、无代理轮换、无签名绕过、无逐条内容审批，也不写正式梗库。

目前持久写入与离线浏览器回归有验证；真实 Linux 账号连续窗口、进程硬退出和告警到达还需服务器上线验收。
