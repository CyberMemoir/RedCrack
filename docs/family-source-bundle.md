# 梗族 source bundle 与平台动作接线

本模块输出`family-source-bundle-v1`，供中央collector 0.5.0导入。它与既有`platform-collection-v1`图导出并存；不修改中央Store、任务队列、正式梗或人工query/gold。采集及规则处理运行时0 AI，不下载媒体。

## 从已保存观察生成自包含包

```powershell
python xhs.py family-bundle --input saved/results.json --bundle output/family-batch --source-id xhs-history-window --sample-mode historical --outcome partial --pause-record pause.json
```

输入支持reader的note.json/results.json、归档目录，或已导出的nodes.jsonl（只读note节点）。输出为manifest.json、UTF-8无BOM的records.jsonl及objects/sha256.bin。manifest和每条source记录均为整数schema_version=1；公开URL规范为`https://www.xiaohongshu.com/explore/{24hex-id}`。包可独立复制导入，不引用原目录。已有包只允许逐字节相同重放，禁止覆盖不同内容。

证据对象是明确标记的allowlist JSON视图，仅保留原样安全标题/正文、身份、原日期和观察时间；texts的JSON pointer精确指向这些字符串。访问URL、Cookie、媒体签名地址、profile及个人路径不复制进包。包含访问令牌或个人路径的整个文本字段被省略并计gap，不将改写文本冒充原文。原始reader私有归档保持不变；此包不包含原始网络响应，未声称raw-response保全。

observed_at必须来自已有带时区观察；未知则跳过该记录并计gap，不能用本次导出时间替代。未知published_at保留null。数字日期不按位数猜单位：通过输入行的`publication_time.basis`或显式`--publication-basis unix_seconds/unix_milliseconds`提供单位；year/month/date和无时区ISO还需offset_minutes（或`--offset-minutes`）。规范化结果含精度、时间区间、basis及指向证据对象原值的source_evidence。未提供basis时保留日期原值及unknown状态，不生成归一化日期。

`--outcome`是reader实际结果；同目录task.json中的停止/失败状态、results.complete=false、已有pause记录和数据缺口只会收紧结果，不将partial/blocked升级为成功。CLI退出0只说明包写出成功，以manifest.outcome判断访问结果。当前合同暂停枚举仅blocked_login/blocked_verification，其他暂停类型显式拒绝，不擅自映射成功。

## 消费search action：当前仅离线fixture执行

```powershell
python xhs.py family-action --state-dir output/action-state enqueue --action tests/fixtures/family_action.json --fixture tests/fixtures/family_pages.json --mode history
# 使用上一步返回的id
python xhs.py family-action --state-dir output/action-state run --recipe-id RECIPE_ID --max-pages 1
python xhs.py family-action --state-dir output/action-state run --recipe-id RECIPE_ID --max-pages 1
python xhs.py family-action --state-dir output/action-state status --recipe-id RECIPE_ID
```

接收action_type=search、target_platform=xiaohongshu、family_id UUID、query、purpose、evidence_refs；保留anchors和priority，规则query必须rule_seed且is_actual_search_log=false。purpose支持find_independent_uses/find_original_reference/find_explainer。fixture文件必须明确sample_mode=fixture；它的游标原样保存，不能用于线上请求。返回bundles中的相对目录位于state-dir下，每页均为可独立导入包。

SQLite平台spool保存recipe、epoch、execution、页包和游标；它不创建中央任务或调度服务。进程退出后再运行同一recipe，沿已提交的原样token继续。先原子发布包再提交游标；若在二者之间硬退出，同执行重放相同包后提交，不重复发布。OS文件锁在进程消失后释放，防止同spool并行worker踩写；stop/cancel仍可由另一进程保存。

head模式每次只取有界首页。完成后用`refresh --recipe-id RECIPE_ID`产生新的epoch/execution，不能复用前一执行身份；fixture观察时间不变，不冒充新平台观察。history保留真实driver给出的next_cursor，末页结束；缺页、重复游标或无进展标gap。当前driver只证明fixture游标恢复，不证明小红书真实token已接通。

`stop --recipe-id RECIPE_ID --reason 说明`和`cancel --recipe-id RECIPE_ID --reason 说明`持久生效；重启、重复enqueue和refresh不会恢复。Ctrl+C/SIGTERM保存停止；硬退出保留checkpoint供同执行恢复。blocked页先保留已获得记录和原outcome，再阻止后续页。取消状态属于动作spool，不伪造合同未包含的cancelled bundle outcome。

## 中央验收与能力边界

将包复制到隔离目录，中央使用独立collector.toml执行import-platform → family-sync → family-list/family-timeline → verify，然后重放import/sync。records SHA、全部objects SHA、原文pointer和日期转换由中央实际重验。fixture标签不代表真实素材，正式入库批准仍由人工流程负责。

| 能力 | 当前状态 |
| --- | --- |
| 自包含producer、SHA/原文pointer、显式时间单位 | 已实现；离线跨仓库导入验证 |
| family search action合同及持久状态 | 已实现；fixture driver消费 |
| 不透明token分页、head新执行、重启/硬退出恢复、停止/取消 | fixture已验证；不是线上分页声明 |
| 正常reader的搜索/详情 | 原实现保留；当前账号暂停，未重新访问 |
| family action自动执行线上搜索 | 尚未接入；不启动浏览器或绕过暂停 |
| 线上search_id/session/token恢复、历史时间筛选、作者/系列枚举 | 未支持；不以数字页码或fixture token代替 |
| 中央dispatcher执行target_platform=xiaohongshu | 仍需中央接线；本PR不改公共dispatcher |

线上action/token、观察时间和额外停止枚举缺口见[issue #7](https://github.com/CyberMemoir/RedCrack/issues/7)；历史热门来源与证据窗口见[issue #6](https://github.com/CyberMemoir/RedCrack/issues/6)。[设备暂停 #3](https://github.com/CyberMemoir/RedCrack/issues/3)及[详情恢复 #4](https://github.com/CyberMemoir/RedCrack/issues/4)保持未解决。历史热门优先，当前累计指标不证明过去热门。已有冻结材料、用户取消及账号设置均不因本接线改变。
