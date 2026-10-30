# 小红书候选持续发现：离线执行合同

B站发现锚点，本路线补source/derivative/usage，不注册新梗族、不做全平台热门发现。`frontier`是独立平台元数据spool，不改中央Store/CLI/dispatcher。本轮CLI只接受offline_replay，拒绝live配置；旧AccessPause原样保留。NormalReaderDriver仅为已有XHSReader.search/get_note的受门禁适配器，未作为本轮live入口开放。

## 可直接复核

从仓库根运行；demo目录必须是新目录。

```powershell
python tests/fixtures/create_frontier_demo.py --directory output/frontier-demo
python xhs.py frontier run --config examples/frontier.offline.json --rounds 4
python xhs.py frontier status --state-dir output/frontier-demo/frontier
python -m unittest tests.test_reader_frontier
```

生成器只建带fixture标签的合成SQLite源和回放，不访问网络。分页token明确为fixture-only，不能送到真实平台。每轮batch=2、source_batch=1、update_batch=1、depth=1只是可复核例子；默认单worker、batch=5、新源20行/旧源5行，详情按稳定note_id去重。原正常reader仍用自身interval/profile预算，不新增网页20秒政策，不下载视频/图片/字幕、不运行模型。

私有配置路径相对于配置文件解析。state_dir必须在B站data目录之外。持续离线运行可不传rounds，但配置必须有deadline_unix（使用主会话B站验收的原deadline，不重置72小时）。有队列就继续处理，空队列本地等待idle_seconds；不另建automation或服务器服务。

## 增量锚点及证据

只读源SQLite（mode=ro/query_only），优先explainer_catalog_works的record_json及已有classification_json；缺该表的旧源/fixture才读observations。这样能接到catalog后来补充的讲解分类，不重新运行分类规则。仅保存的explicit_candidate（旧源strong_candidate）生成锚点/任务；其他行也推进读取水位。

new_watermark沿选定表的rowid推进；update_watermark对已读行做有界循环复核，source_versions记录行id及记录/分类快照SHA。源没有通用updated_at索引，因此旧行/仅分类变化在回查游标到达后发现，不承诺实时changefeed，也不每轮全库扫描。新观察（即使正文SHA没变）或记录变化给查询新的epoch；重复读取同一行版本不重复派发。每个frontier绑定源库及表，切换表需新frontier，禁止把observations旧水位套到catalog。

标题/简介逐项用原JSON pointer核对SHA对象。清洗规则只去em高亮、解HTML实体和已知讲解标题前缀；证据同时保存原文、cleaned_text及规则，不把清洗后的偏移冒充原文pointer。支持“是什么梗/是啥梗”等标题subject候选，以及原句/出自/源自/原作后的明确引号片段；其他模糊名称仍在原观察中，不猜查询。B站显式小红书链接优先详情；未支持的短链解析不虚构目标ID。

候选anchor身份为bilibili:BVID、family_id=null；若源记录明确给出合法UUID则保留引用，不新建family。source_versions/objects及每条path保留多个观察窗口和原文，anchor的当前快照更新不会删除旧发现路径。平台库不自动family-sync或公开入库。

## Frontier与作品身份

本地frontier.sqlite3分别存anchors、jobs、paths、cards、works、access、roles和receipts。公开来源URL规范为/explore/note_id，原访问token/source-card数据位于私有access映射；原观察JSON对象也仅在私有state目录。一个作品可有多个BVID/已知family和多条搜索/引用路径；URL去重不删paths。已经取得详情的作品收到新锚点路径时，会重新关联已有证据而不重复读取，并沿有界显式引用补路径。

队列优先明确链接，在head/history两lane间持久轮转，分页延续进入history。搜索卡片另存，不升格详情；详情必须同ID且scope=detail，ReaderError不可用项不写works。原生related接口未实现，只扩展实际正文中的明确小红书引用（每观察最多2链接，默认1层，最大2层）。源链接每字段最多3个、源查询每观察最多3个；这些是显式有界处理范围，不声称全量链接覆盖。

每个回放页原样保存cursor/next_cursor；耗尽、无匹配、重复cursor/环与unsupported均有状态。真实正常search跨进程cursor/session合同仍unsupported；NormalReaderDriver仅有界head，不能把fixture token或B站页码拼进请求。真实搜索/详情未在本轮访问，不把离线结果升级live_verified。

角色证据均是candidate_not_confirmed、origin_confirmed=false。明确B站链接/引用可形成source引用候选，不等于起源；derivative必须有改词/剪辑/翻唱等动作和锚点目标文字，usage需目标及用法/模板等线索，保留各自原文位置。两角色可重叠；只有同词而无动作/用法证据时保留unknown。合集、歧义和文本不足没有被强判父子关系。作品发布时间及本次指标快照保持原值，不要求历史热度证明。

## STOP、崩溃与巡检

```powershell
python xhs.py frontier stop --state-dir output/frontier-demo/frontier --reason "人工停止"
python xhs.py frontier status --state-dir output/frontier-demo/frontier
# 以下只能在新的明确操作授权后执行，不是重启默认动作
python xhs.py frontier resume --state-dir output/frontier-demo/frontier --reason "明确恢复离线试跑"
python xhs.py frontier resolve --state-dir output/frontier-demo/frontier --job-id JOB_ID --decision abandon --reason "已人工核对未知在途"
```

单worker OS锁在进程消失后释放。CLI stop、STOP文件、Ctrl+C/SIGTERM持久保存停止；移除STOP文件也不自动清除已存停止。resume只解除本spool的操作停止，不处理AccessPause或platform_stop。重启会把未知inflight标为uncertain，跳过它们；只有显式resolve/retry/abandon和reason才能改变，不能因重启盲重试。代码错误保存uncertain/故障并停止，run返回退出码2，不伪造成功。

status使用只读SQLite，不建队列、不发请求。记录的worker PID为实际Python进程，Windows venv启动器PID可能不同；硬退出后的PID仅是最后记录，不能单凭非空认定存活。主会话用已有半小时巡检读取state.json/receipts即可，不新建自动化。到deadline持久停止，用户手动停止不自动恢复。

每轮收据在receipts表及receipts/NNNNNNNN.json；state.json保存最后状态、水位、PID、命令、停止与deadline。记录新增/更新锚点、驱动调用、搜索卡片、唯一详情、重复卡片、无匹配/不可用/未知、角色路径及去重作品数、lane/cursor/耗尽/停滞。offline平台请求始终0，driver_calls是模拟步数；未来normal reader的HTTP总数未测时为null，不冒充精确请求数。

| 能力 | implemented | offline_verified | live状态 |
| --- | --- | --- | --- |
| 持久frontier/多路径/稳定note去重 | 是 | 是 | 未启动live |
| B站新增水位/旧行有界复核/新head epoch | 是 | 是 | 本地只读源，无平台请求 |
| head/history公平轮转、分页推进/停滞 | 是 | replay | 真实搜索cursor unsupported |
| 同ID完整详情、source/二创/用法候选 | 是 | replay及原reader离线检查 | 旧账号暂停live_blocked |
| STOP/重启/unknown inflight/单进程锁 | 是 | 是 | 未启动live |
| 原生related/作者/话题分页 | 否 | 否 | unsupported，缺正常入口合同 |

平台账号暂停由既有设备登录上限历史报告产生，不等于本轮新验证码或AccessBlocked。此实现可继续离线验证；恢复访问需后续明确处理并授权。服务器常驻部署、中央资源租约及dispatcher都不在本PR范围。
