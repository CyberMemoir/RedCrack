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

队列优先明确链接，在head/history两lane间持久轮转，分页延续进入history。搜索卡片另存，不升格详情；详情必须同ID且scope=detail，ReaderError不可用项不写works。related独立任务已接入队列，但真实入口合同缺失，仅显式related fixture启用；正常reader不会把首页推荐、评论或正文引用当related。related响应须绑定parent_note，保留多父笔记/多锚点路径，子详情按稳定ID共用；后到的锚点路径可贯穿已缓存的多页结果，不重新读取。related本身只证明发现路径，角色初始unknown。扩展共用默认1层、最大2层界限，每页最多20卡片。正文明确小红书引用仍每观察最多2链接；源链接每字段最多3个、源查询每观察最多3个。

每页持久保存cursor/next_cursor和私有page_results；耗尽、重复cursor/环、重复页、无新增ID、空页仍声称has_more、矛盾耗尽元数据分别保存原因并停止该分支。终页无结果记录no_match及entry_exhausted；不自动重试停滞分支。

原生搜索接线见下节。NormalReaderDriver仍只做原有有界head；NativeSearchDriver是独立受控适配器，CLI仍拒绝live配置。真实搜索/详情未在本轮访问，不把离线结果升级live_verified。

## 原生搜索合同与缺失证据

已有公开项目源码`request/web/apis/note.py::Note.search_notes`给出POST `/api/sns/web/v1/search/notes`及keyword/page/page_size/search_id/sort/note_type/ext_flags/geo/image_formats。它是页码合同，不是评论cursor。`native_search.search_request/search_page`据此实现参数传播：从实际捕获的请求body起步，原样保留过滤参数及未知扩展字段、同一search_id，只将page加1。保存的next_cursor是`{contract: xhs-search-page-v1, request_body: ...}`，不拼接fixture游标或B站页码。缺search_id、关键词不符、非正整数page、page_size超20均拒绝。

返回结构data.items及has_more/hasMore来自已有正常reader解析代码；has_more必须显式boolean，缺失时停止，不能猜耗尽。真实接口是否仍遵守这些字段尚无本轮响应证明。NativeSearchDriver只接收调用方注入的transport及捕获请求seeds，先查AccessPause、默认permit_live=false，再沿用ProfileRuntime锁/interval/预算。它不创建登录会话、不新增签名逻辑；request/response SHA关联存私有evidence。未来transport需用获准的现有会话，不应通过改写已签名请求来接线。HTTP限制/行为提醒持久暂停；登录失效停止。此PR没有把它接成可运行的正常Chrome原生请求发送器，CLI仅运行offline_replay。

已有本地档案审计109个JSON：0个正常search-response、0个search-context；32处cursor属于详情评论，不能证明搜索翻页。尚需恢复后采到：相同关键词/过滤条件的连续两页正常请求body（page、page_size、search_id及扩展字段）、对应响应data.items/has_more、下一页同会话行为及终页。源码没有证明search_id的服务器寿命/跨进程有效期，失败需停止，不自动换ID或重新登录。

related仍缺：可确认的正常页面入口/请求path、method、父note_id绑定字段、会话/访问字段、分页字段、响应卡片容器及has_more语义；也缺两父笔记实际指向同一子笔记的响应证据。没有猜endpoint或把homefeed推荐当相关推荐。队列/解析后的卡片交接/分页/多父去重可离线验证，真实请求与原始响应解析待上述合同；作者/话题分页不在本轮接线范围。

新的可复现合成验收（所有ID/session/related页均为fixture）：

```powershell
python -m tests.fixtures.create_native_frontier_demo --directory output/native-demo
python xhs.py frontier run --config output/native-demo/config.json --rounds 8
python xhs.py frontier status --state-dir output/native-demo/frontier
```

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
| 原生search page/search_id参数、持久推进/停滞 | 是，源码合同+注入transport | 合成响应；非真实响应 | live_blocked；捕获seed及正常Chrome transport未接通 |
| 同ID完整详情、source/二创/用法候选 | 是 | replay及原reader离线检查 | 旧账号暂停live_blocked |
| STOP/重启/unknown inflight/单进程锁 | 是 | 是 | 未启动live |
| related独立任务/分页/多父路径及详情去重 | 是，解析后卡片交接 | fixture；不是原生合同证据 | 真实入口及原始响应解析contract_missing |
| 作者/话题分页 | 否 | 否 | unsupported |

平台账号暂停由既有设备登录上限历史报告产生，不等于本轮新验证码或AccessBlocked。此实现可继续离线验证；恢复访问需后续明确处理并授权。服务器常驻部署、中央资源租约及dispatcher都不在本PR范围。
