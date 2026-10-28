# 持续关联采集的平台交换接口

本仓库提供正常网页reader与离线转换器。持久任务队列、数据库、跨平台资源租约、下载配额、OCR和长期调度由CyberMemoir共享采集服务负责。

向中央梗族事件服务传递原文观察，使用独立的[family-source-bundle producer与平台动作接口](family-source-bundle.md)；图导出不替代自包含证据包。

```powershell
python xhs.py collection-export --input downloads/window-01 --output output/exchange --tool-revision <commit>
# 已有暂停时仍可离线整理；省略参数不会清除该输出中已保存的暂停
python xhs.py collection-export --input downloads/window-01 --output output/exchange --pause-record pause.json
```

输入为既有`results.json`、`note.json`或归档目录。保持reader与原JSON契约：`Note.url`和原JSON是私有访问数据，`Note.canonical_url`才是公开来源链接。CSV/Markdown的来源链接使用canonical；不要公开原JSON、缓存、profile和证据快照。

## 搜索卡片到详情

页面卡片现在保留所有候选href，按同一note ID选择带访问参数的链接，不能把其他卡片的token拼到目标ID。
来源页及完整访问URL保存在私有`access`与独立access缓存，response提供的已知xsec_source保持绑定。
当卡片没有token时，get_note会回到真实来源页，在匹配该ID的卡片上执行正常点击；不将canonical URL强当成可访问入口。来源卡片不可见时明确失败，不猜链接、不无限翻页。

详情可以由网页正常feed响应或noteDetailMap提供，即使弹窗没有改变搜索页URL；必须匹配目标ID。非笔记跳转、其他ID、登录/错误页都不算详情。
`content_scope=search_card/detail/unknown`区分卡片与详情。图集和视频只有经详情读取后才可标为detail；搜索封面不成为完整图集或OCR输入。
多个来源卡片顺序读取时会正常恢复来源页，恢复访问同样计入原搜索预算；重启仍保留source-card访问引用与缓存观察时点。

## platform-collection-v1文件

所有公开JSONL记录和JSON根对象使用整数`schema_version: 1`，协议名称单独保存在`schema_name: "platform-collection-v1"`。`edges.from/to`使用`{platform, entity_type, entity_id}`身份对象，并与nodes身份一致；未读取正文的显式链接目标用`content_scope=reference_only`节点表示。导出前检查这些契约。

旧版字符串schema_version导出不原地升级或混写；请对同一批输入指定新的output目录，保留旧导出及原始证据。新目录同输入重跑仍然幂等。离线重导出不是新增采集。

| 文件 | 职责 |
| --- | --- |
| `nodes.jsonl` | note/author/tag及正文显式链接到的note身份；canonical URL、原时间、字段、指标和证据定位 |
| `edges.jsonl` | 有真实作者ID的author_post、真实tag ID的tag_member、原文明确链接的links_to；不推断derivative/origin |
| `frontier.jsonl` | 缺少详情的note、原文明确引用note的后续候选，供共享队列接线；它不是已执行遍历 |
| `media_manifest.jsonl` | 只有明确讲解句式命中的detail材料，提供素材索引、私有access_ref和证据；不会下载、转录或运行OCR |
| `queries.jsonl` | 标题/正文的需求原句副产物，real_expression，不是实际搜索日志或自动确认的梗名 |
| `capabilities.json` | 入口能力、版本与验证状态；离线转换不能把入口升为available |
| `trial_summary.json` | 离线转换状态与实际访问状态分开；本命令platform_requests/media_bytes/model_calls均为0 |
| `resume.json` | 已导出身份、版本计数和输入指纹；同输入重跑幂等，已存节点版本不覆盖 |
| `private/` | 完整访问/媒体地址、输入映射、精确query上下文与原文、暂停副本；不可公开 |

公开文本中的小红书访问URL会去掉临时参数，原文精确切片仍由私有证据指纹和locator指向。不要用公开脱敏文本的位置替代原始证据位置。没有真实tag ID时只保留名称，不能用名称伪造平台稳定ID。未知observed_at保留null，不以导出时间或发布时间代替。

`explanation_candidate`只是版本化规则候选：标题/正文有“梗科普/解读”等明确讲解标志，或“是什么梗”题名配合正文解释句，才生成详情素材manifest；不证明该内容讲解准确。只有名称问句和话题标签时仍只是需求线索，不触发媒体任务。普通内容仍只有元数据，不生成全量媒体下载任务。没有原生字幕或完整媒体时不猜URL、不调用模型补空值。

## 能力与公共接线缺口

当前search/account_recommendations/note_detail有实现；本轮离线导出默认unverified，有未处理暂停时blocked。作者历史列表、评论和平台搜索建议尚未实现；标签页、相关推荐没有可验证入口，标unverified。
作者和标签元数据节点并不意味着已遍历作者投稿或标签页；正文links_to也不等于平台推荐边。

旧结果有keyword时另存`discovered_via.entry_type=search`、`layer=rule_seed`。当前交换契约的节点类型没有search/entry，因此不会伪造一个tag来承载search_result边；共享队列需提供统一入口节点身份或接受discovered_via字段。平台真正建议未来才标platform_search_clue。
共享服务需保管`access_ref -> private/access.jsonl`映射，并用reader恢复访问；不能只拿canonical URL重开详情。frontier候选执行前仍须检查平台暂停、能力与中央预算。本转换器不另建worker或自动执行这些候选。

## 历史热门优先与下一阶段

赛博回忆录首先回填过去曾流行的meme及笔记/视频用例，当前热榜和近期推荐只作补充。来源优先级为：按年/月时间窗整理历史榜单与精选；回溯作者/系列旧投稿及历史事件、梗名、别称和需求原句；定位同期原帖、报道和讨论记录。后来的盘点、回顾和讲解只提供回溯线索，需继续寻找原作及同期证据。高累计指标本身也不构成正式梗条目的收录依据。

历史热门证据区分同期榜单/精选及排名时间、同期互动快照/可定位传播记录、后来的回顾声称。当前累计赞或播放不能证明发布当年热门；回顾声称不自动升级为同期热门事实。推荐边只表示本次发现关系，不能当作历史传播链。

下一阶段需与共享契约协调，分别保存作品发布时间、历史流行时间窗（可多个）、证据发表时间、指标采样时间、本次采集时间，未知留空。原始发布、首次有证据的走红、后续翻红分别记录。现有`published_at_raw`、`observed_at`、`metrics`尚未实现这五类时间和历史热门证据结构；导出`recorded_at`也不能补作历史观察时间。

当前没有支持的历史枚举、搜索时间筛选、作者/系列历史分页或历史榜单导入能力；标签/相关笔记入口仍未验证。按年份关键词搜索也不等于完整覆盖该年份。缺口与后续范围见[issue #6](https://github.com/CyberMemoir/RedCrack/issues/6)：先核对真实入口及能力状态，再实现有预算的分页/时间窗断点与证据字段，不在本轮扩展运行时或修改冻结数据。

恢复访问并取得后续试采范围后，先取多个年份/时间段的小样本，统计各年份取得的作品、带同期热门证据的作品、回顾线索、原始用例/独立作者、历史缺页/失效和未知流行时间比例。没有可核对全集时不报告全平台历史覆盖率。离线导出成功不代表历史发现或持续爬取已完成；运行时保持0 AI。

## 暂停与验证

访问限制HTTP403/412/429/461/471/567、已识别设备/行为提醒与验证码持久暂停。捕获到另一reader端点的限制响应也不能因当前等待feed而丢弃。
历史暂停不会因新任务、换连接模式、缓存成功或页面能打开而自动清除。输入pause.json使用blocked_login/blocked_verification/blocked_rate_limit之一；状态与原始说明保存在private目录，后续不带参数重跑仍保持暂停。
人工处理后需按原reader的acknowledge-warning流程显式记录恢复依据。离线通过不等于线上账号可用，更不等于生产持续窗口验收。
