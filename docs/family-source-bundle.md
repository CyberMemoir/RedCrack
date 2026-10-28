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

## 消费search action：离线fixture与有界正常reader

```powershell
python xhs.py family-action --state-dir output/action-state enqueue --action tests/fixtures/family_action.json --fixture tests/fixtures/family_pages.json --mode history
# 使用上一步返回的id
python xhs.py family-action --state-dir output/action-state run --recipe-id RECIPE_ID --max-pages 1
python xhs.py family-action --state-dir output/action-state run --recipe-id RECIPE_ID --max-pages 1
python xhs.py family-action --state-dir output/action-state status --recipe-id RECIPE_ID
```

接收action_type=search、target_platform=xiaohongshu、family_id UUID、query、purpose、evidence_refs；保留anchors和priority，规则query必须rule_seed且is_actual_search_log=false。purpose支持find_independent_uses/find_original_reference/find_explainer。fixture文件必须明确sample_mode=fixture；它的游标原样保存，不能用于线上请求。返回bundles中的相对目录位于state-dir下，每页均为可独立导入包。

正常reader的head调用（只在已授权且无暂停时执行；本轮未联网验证）：

```powershell
python xhs.py family-action --state-dir output/normal-head enqueue --action action.json --reader-profile EXISTING_PROFILE --connect-existing --mode head --limit 2 --detail-limit 1 --max-scrolls 0
python xhs.py family-action --state-dir output/normal-head run --recipe-id RECIPE_ID
```

使用原XHSReader.search/get_note，不建另一套网络客户端。先检查原profile暂停，再构造reader；命中暂停仍输出0记录blocked包且不连接浏览器。默认最多2卡片、1详情、0次滚动；上限5卡片、详情不超过卡片、滚动0–2，绝不下载媒体。详情必须同ID且content_scope=detail，失败则保留卡片原scope与失败outcome。source记录保留不透明access_ref，映射位于每次执行的private/access.json及权威checkpoint内；访问URL/token不入公共包。

搜索完成与每条详情完成后持久checkpoint。进程重启跳过已完成搜索/详情，重建原访问缓存，再继续剩余详情；包发布后中断可复用原包。不完整搜索没有可恢复真实token，硬退出后的再运行只能重新执行有界head并去重，不能称为history续跑。refresh新epoch显式刷新缓存。运行期间持久stop/cancel由控制循环传给在途reader并正常关闭本次连接；不会关闭复用的用户Chrome。原始网络证据保存在执行private/evidence下，正常搜索的真实post JSON与响应SHA单独写search-context，不捕获授权头，也不据字段名拼接新请求。

`--reader-profile ... --mode history`会在入队前拒绝；当前仅fixture driver支持原样token分页。原有fixture CLI及spool保持兼容。无中央调度或daemon也可独立运行有界命令。

SQLite平台spool保存recipe、epoch、execution、页包和游标；它不创建中央任务或调度服务。进程退出后再运行同一recipe，沿已提交的原样token继续。先原子发布包再提交游标；若在二者之间硬退出，同执行重放相同包后提交，不重复发布。OS文件锁在进程消失后释放，防止同spool并行worker踩写；stop/cancel仍可由另一进程保存。

head模式每次只取有界首页。完成后用`refresh --recipe-id RECIPE_ID`产生新的epoch/execution，不能复用前一执行身份；fixture观察时间不变，不冒充新平台观察。history保留真实driver给出的next_cursor，末页结束；缺页、重复游标或无进展标gap。当前driver只证明fixture游标恢复，不证明小红书真实token已接通。

`stop --recipe-id RECIPE_ID --reason 说明`和`cancel --recipe-id RECIPE_ID --reason 说明`持久生效；重启、重复enqueue和refresh不会恢复。Ctrl+C/SIGTERM保存停止；硬退出保留checkpoint供同执行恢复。blocked页先保留已获得记录和原outcome，再阻止后续页。取消状态属于动作spool，不伪造合同未包含的cancelled bundle outcome。

## 中央验收与能力边界

将包复制到隔离目录，中央使用独立collector.toml执行import-platform → family-sync → family-list/family-timeline → verify，然后重放import/sync。records SHA、全部objects SHA、原文pointer和日期转换由中央实际重验。fixture标签不代表真实素材，正式入库批准仍由人工流程负责。

| 能力 | 当前状态 |
| --- | --- |
| 自包含producer、SHA/原文pointer、显式时间单位 | 已实现；离线跨仓库导入验证 |
| family search action合同及持久状态 | 已实现；fixture或正常reader head消费 |
| 不透明token分页、head新执行、重启/硬退出恢复、停止/取消 | fixture已验证；不是线上分页声明 |
| 正常reader的搜索/详情 | 原实现保留；当前账号暂停，未重新访问 |
| family action调用正常search/detail reader | implemented、offline_verified；当前blocked，尚无本轮live_verified |
| 线上search_id/session/token恢复、历史时间筛选、作者/系列枚举 | 未支持；不以数字页码或fixture token代替 |
| 中央dispatcher执行target_platform=xiaohongshu | 仍需中央接线；本PR不改公共dispatcher |

线上action/token、观察时间和额外停止枚举缺口见[issue #7](https://github.com/CyberMemoir/RedCrack/issues/7)；历史热门来源与证据窗口见[issue #6](https://github.com/CyberMemoir/RedCrack/issues/6)。[设备暂停 #3](https://github.com/CyberMemoir/RedCrack/issues/3)及[详情恢复 #4](https://github.com/CyberMemoir/RedCrack/issues/4)保持未解决。历史热门优先，当前累计指标不证明过去热门。已有冻结材料、用户取消及账号设置均不因本接线改变。

## 最小离线复核与能力证据

```powershell
python -m unittest tests.test_reader_family_reader tests.test_reader_family_bundle tests.test_reader_cli
python xhs.py reader-audit --input saved/raw --report output/private-reader-audit.json
```

审计仅遍历原JSON，输出SHA、字段pointer/类型/所在作用域，不输出token值。2026-10-28本地限定3个既有材料目录的109份JSON：没有保存normal search-response/search-context，32处cursor均位于详情comments状态，不能复用为搜索页token；没有search/session请求合同。保存note中有32个不同author_id，可作待核对的作者身份，不能证明存在可读旧投稿分页入口。此为该批归档审计，不声称全平台没有相应接口。

| 能力 | implemented | offline_verified | live_verified | unsupported | blocked |
| --- | --- | --- | --- | --- | --- |
| family head→正常搜索→同ID详情→包/访问引用 | 是 | 是（替换search/detail返回的正常reader上下文） | 否 | 否 | 既有账号暂停 |
| head刷新新身份、搜索/详情checkpoint恢复 | 是 | 是 | 否 | 否 | 同上 |
| 正常搜索请求/响应上下文保存 | 是 | 是（合成捕获响应） | 否 | 否 | 同上 |
| 跨进程恢复真实搜索cursor/session | 否 | 否 | 否 | 是 | 缺证据及暂停 |
| 作者旧投稿/话题分页 | 否 | 否 | 否 | 是 | 无验证入口 |
| 历史热门枚举/日期筛选 | 否 | 否 | 否 | 是 | 缺历史来源合同 |

已发布时间较早只证明“旧内容”；今天累计高互动只证明本次指标采样值。过去曾热门仍需同期榜单、同期互动/传播证据或明确标记的回顾声称，综合搜索翻页、当前热词和推荐共现都不能替代。
