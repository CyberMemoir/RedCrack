# 复用正在运行的Chrome登录状态

`--connect-existing`通过Playwright连接用户已授权的Chrome会话，不启动独立浏览器、不导入Cookie。
连接机制依据Chrome 144+的[官方会话连接流程](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)，以及官方ChromeDevTools项目的[连接实现](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/src/BrowserManager.ts)。
运行时仍为普通Python/Playwright，不调用AI或要求AI会话驱动。

## 准备与执行

1. 在平时使用、已登录小红书的Chrome中打开`chrome://inspect/#remote-debugging`，启用远程调试。
2. 执行下面的命令，Chrome弹出连接请求时点“允许”。授权可访问该profile的窗口；工具只新建自己的采集页。

```powershell
python xhs.py search "求表情包原图" --connect-existing --timeout 90 --refresh --limit 1 --max-scrolls 0 --profile ../raw/browser-profile -o output/existing-check
```

Windows默认从`%LOCALAPPDATA%/Google/Chrome/User Data`读取`DevToolsActivePort`连接元数据；环境未提供LOCALAPPDATA时使用当前用户的`AppData/Local`。
如果Chrome使用自定义目录，用`--chrome-data-dir "Chrome数据根目录"`指定。
该目录是`chrome://version`中“个人资料路径”的父目录（例如路径末尾为Default或Profile 1时，取其上一级），不是RedCrack的`--profile`目录。
不会读取Chrome的Cookie或密码数据库、复制浏览器资料、扫描端口，或在连接失败后改用新浏览器。

`--profile`在此模式只用于RedCrack的串行锁、原预算、暂停记录与缓存，应继续使用原目录。
`--headless`、`--browser-channel`不会改变已运行Chrome的窗口模式。
`--refresh`用于明确进行真实读取；没有它时仍遵守既有缓存规则，缓存成功不证明当前登录态有效。

## 生命周期

连接后在已有浏览器上下文中新建一个采集标签页，读取正常网页的搜索/详情响应；保留访问预算和验证停止行为。
任务结束只关闭自己创建的页面，再停止本地Playwright连接；不调用用户浏览器或其上下文的close，不关闭原有标签页。
每次重新建立连接，Chrome可能再次要求用户授权。没有开启授权入口时明确失败，不自动修改Chrome设置。

当前实现要求Chrome暴露有效的DevToolsActivePort文件。只有调试管道、没有该连接入口的实例不能据此自动接管，需确认用户正在使用并授权的是哪个Chrome实例。

本机复用登录与服务器无人值守是不同的部署条件。此次新连接方式尚需真实会话检查，不能用离线生命周期测试替代线上登录与采集成功。
