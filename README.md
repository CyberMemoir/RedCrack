# RedCrack

> [CyberMemoir](https://github.com/CyberMemoir) 项目的一部分；CyberMemoir 是由 [Cogstruct AI](https://github.com/Cogstruct-ai) 开发和维护的开源互联网文化记忆计划。

**小红书自动搜索、批量下载与本地归档工具。** 使用 Python + Playwright，通过独立浏览器保存登录状态，读取网页自己的搜索结果和笔记详情；无需手动填写 Cookie 或维护请求签名。保留原有自动发布模块。

## 功能

常驻服务器使用 [生产采集接入说明](docs/server-collection.md) 中的共享队列；以下命令是有界读取入口。真实账号连续运行仍待服务器验收。

- **推荐页发现**：`python xhs.py discover --limit 5 --max-scrolls 2 --headless -o output/feed-window`，只读取当前账号推荐页，不声称全站热门覆盖。保存正文卡片、来源证据与`discovery.json`入口信息；不下载媒体、不调用模型。搜索与推荐缓存分开，共享原有访问预算、串行锁与验证暂停规则。

- **关键词搜索**：自动滚动翻页、按笔记 ID 去重、保留完整访问链接。
- **批量输入**：笔记 URL、24 位 ID、`xhslink.com` 短链接、分享文案、文本清单、搜索 JSON。
- **图片与视频**：下载详情中的图片及直链视频；视频优先选择 H.264 中分辨率最高的可用版本。
- **本地归档**：正文 Markdown、完整元数据 JSON、可直接用 Excel 打开的 UTF-8 BOM CSV。
- **可重复执行**：流式写入、原子替换、SHA-256 完整性校验；再次运行跳过已完成的相同媒体。
- **失败可追踪**：单素材失败不影响其他素材；每篇下载清单、整批执行记录和明确退出码。
- **登录复用**：首次扫码，之后可使用 `--headless`；登录与下载文件不纳入 Git。

## 快速开始

### 收到行为提醒后

优先停止自动访问并按官方页面处理提醒。真实浏览器不保证平台允许自动操作；
本工具不提供隐藏自动化、指纹伪装或验证码绕过，也不保证任何频率不会触发提醒。

保持全自动，支持有限关键词清单：

```bash
python xhs.py search "狼王撕衣" --download -o downloads/wolf
python xhs.py search --queries-file queries.txt --download -o downloads/batch
```

`queries.txt` 每行一个关键词，空行和 `#` 注释忽略，处理完清单即结束。
搜索结果按笔记ID合并，完整详情独立缓存；不会把搜索封面当成全图集。
默认复用成功取得的搜索/详情；显式 `--refresh` 才重新访问，刷新和失败尝试同样计入预算。
输出的 `observed_at` 是本地原读取时点，`cached` 表示缓存复用，互动计数可能过时。
既有批次输出不会自动反向导入缓存；缓存只复用此版本正常保存的材料。

同一profile跨命令共享访问预算和文件锁：每段最多3次搜索、12篇详情或10分钟，
任一达到后休息20分钟，再自动接续。每次搜索最多自动翻页2次，无新增结果提前结束。
浏览器访问串行，媒体默认并发1；所有主动网络动作共享最小间隔，默认15秒，
页面内容就绪后才继续，不按固定秒数机械点击。休息等待分段检查暂停记录，可中断。
暂时连接故障最多重试1次；账号提醒/验证/限制不自动重试。不能保证识别所有延迟提醒。

重复运行相同命令可接续：已完成搜索和详情优先复用缓存，已完成媒体验证指纹后跳过，
失败/中断部分保留逐条记录。中断的搜索可能需要重新打开当前关键词；不会把未完成搜索缓存成完成。
正常任务结束或中断均释放锁，预算仍保留，不能靠重启绕过批间休息。

已识别的行为提醒、验证码或访问限制会在 profile 中写入暂停记录；
退出或重启不会自动重试。人工处理提醒后，可显式确认工具暂停：

```bash
python xhs.py acknowledge-warning --profile "自己的独立资料目录" --reason "已在官方页面检查并处理提醒"
```

这只记录人工确认，不访问平台，也不保证账号限制已经解除。
提醒截图、发现时间和此前动作历史保存在profile内；不把最后一个操作当成触发原因。
动作日志不保存Cookie/访问令牌；profile和原始归档仍包含登录/访问资料，不能提交到Git。
以上是工作量控制默认值，不是平台公布的安全阈值，未验证能减少识别或保证账号安全。

需要 **Python 3.11+**。在 macOS / Linux 中：

```bash
git clone https://github.com/CyberMemoir/RedCrack.git
cd RedCrack
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-reader.txt
python -m playwright install chromium

# 首次打开浏览器，自己扫码登录
python xhs.py login

# 搜索并下载前 20 篇可获取的笔记
python xhs.py search "咖啡探店" --limit 20 --download --output downloads/coffee
```

Windows 激活命令为 `.venv\Scripts\Activate.ps1`。浏览器默认优先使用已安装的 Chrome，找不到时使用 Playwright Chromium；可用 `--browser-channel chromium` 固定通道。

### 只搜索并导出

```bash
python xhs.py search "杭州周末" --limit 50 --headless -o downloads/hangzhou
```

`results.json` 与 `results.csv` 在抓取过程中增量更新。纯搜索导出的是搜索卡片信息，不保证含完整正文或全部媒体；使用 `--download` 或下一节的 `--metadata-only` 读取详情。

### 下载单篇或多篇笔记

```bash
# 保留分享链接中的 xsec_token；链接包含 & 时必须加引号
python xhs.py download "https://www.xiaohongshu.com/explore/笔记ID?xsec_token=访问令牌"

# 支持短链接、包含链接的整段分享文案，以及多个链接
python xhs.py download "https://xhslink.com/a/分享路径" "另一个笔记完整链接"

# 文本清单，每行一个链接；空行和 # 注释行跳过
python xhs.py download --file links.txt --headless -o downloads/batch

# 读取搜索结果，逐篇刷新详情与媒体链接后下载
python xhs.py download --from-json downloads/hangzhou/results.json --headless -o downloads/hangzhou

# 只归档完整标题、正文与元数据
python xhs.py download --file links.txt --metadata-only -o downloads/notes
```

以上链接中的中文部分是需要替换的占位符。裸笔记 ID 不一定能访问，优先使用带 `xsec_token` 的完整分享链接。

### 输出结构

```text
downloads/coffee/
├── results.json             # 搜索或详情结果；包含 complete 标记
├── results.csv              # 标题、作者、正文、链接、互动数据等
├── batch.json               # 使用下载命令时的逐篇执行状态
└── <note_id>/
    ├── note.json            # 完整结构化笔记及媒体地址
    ├── note.md              # 标题、作者、来源、正文、标签
    ├── downloads.json       # 每个素材的状态、文件名、大小、SHA-256
    ├── 001-image.webp
    └── 002-image.jpg
```

视频文件使用 `001-video.mp4` 等名称，扩展名按实际文件头识别，不会把错误页保存成素材。输出目录以笔记 ID 命名，避免标题中的特殊字符、同名或路径穿越问题。

再次执行相同命令即可补下载失败文件。已完成文件会核验大小与 SHA-256；只变化查询参数的临时媒体链接仍可复用文件。**这是文件级续跑，不是 HTTP Range 字节级断点续传。** 同一输出目录不要同时运行多个下载进程。

### 常用参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--limit` | `5` | 搜索结果数量上限，不保证一定返回足量结果 |
| `--max-scrolls` | `2` | 每次搜索最多自动翻页2次 |
| `--interval` | `15` | 搜索滚动 / 逐篇读取的间隔秒数；不是安全阈值 |
| `--timeout` | `30` | 页面和结果的等待超时秒数 |
| `--concurrency` | `1` | 单篇笔记的媒体下载并发数；仍共享访问节奏 |
| `--retries` | `1` | 媒体服务器临时错误后的重试次数，上限1 |
| `--max-size-mb` | `512` | 单个媒体文件的最大大小（MiB） |
| `--profile` | `~/.redcrack/xhs_reader_profile` | 独立浏览器登录目录 |
| `--headless` | 关闭 | 无窗口执行，首次扫码不要使用 |
| `--browser-channel` | `auto` | `auto` / `chrome` / `chromium` / `msedge` |
| `--refresh` | 关闭 | 明确刷新搜索/详情缓存，计入预算 |
| `--queries-file` | 无 | 搜索的有限关键词清单 |

详细参数：`python xhs.py --help`、`python xhs.py search --help`。

正常结果摘要输出到 stdout，进度和错误输出到 stderr。退出码：`0` 成功，`2` 执行失败或部分下载失败，`3` 需要登录，`4` 需要网页验证，`130` 用户中断。`complete` 表示该次批次已结束，不等于所有媒体都成功；素材成功与否以 `downloads.json` / `batch.json` 的 `status` 为准。

搜索/下载另写 `task.json` 和独立 `tasks/<task_id>.json`：区分 `running`、
`complete`、`partial`、`login_required`、`verification_required`、`network_failed`、
`failed`、`interrupted`，保存开始/结束时点和退出码。强制结束进程可能留下 `running`，
不能视作成功。页面、翻页和响应体等待超时停止当前任务；已有结果保留，未完成搜索不写成功缓存。

笔记的 `url` 是本地保留的访问链接，`canonical_url` 是不含访问令牌的稳定来源链接。
互动字段保留原显示值，缺失为 `null`，不以0填充。正文保持原句，不将标题/评论标为真实搜索日志。
`evidence` 提供响应、页面详情状态或可见卡片的本地 JSON 路径、SHA-256、JSON pointer 和读取时点。
CLI 原材料保存在输出目录的 `evidence/`；Python 默认在 profile 的 `reader-evidence/`，
可通过 `XHSReader(evidence_dir=...)` 指定。它们是解析后的网页原数据快照，未声称保存原始HTTP字节，
可能含临时访问/媒体链接，不随代码提交。缓存复用保留原观察时间和证据引用；旧缓存未有证据时不补造。

## 登录与适配

- 搜索模块和原有发布模块使用**不同 profile**，需要各自登录。不要同时用两个进程打开同一个 profile。
- 登录过期时执行 `python xhs.py login`。遇到页面验证时停止本次任务，在有窗口模式下处理后再执行；不会自动解验证码。
- 读取的是当前网页提供的媒体版本，不声称为原图、无水印或所有视频格式；仅有 HLS/DASH 的视频会报告未支持，不会把封面冒充视频下载成功。
- 搜索结果按网页默认相关性排序；没有提供时间范围、作者主页全集、评论采集或定时调度功能。
- 小红书网页不是稳定的公共 API，字段和选择器变化后可能需要更新 `request/reader/browser.py` 与 `models.py`。

## Python 调用

```python
import asyncio
from request.reader import XHSReader
from request.reader.downloader import MediaDownloader

async def main():
    async with XHSReader(headless=True) as reader:
        notes = await reader.search("咖啡", limit=5)
        async with MediaDownloader("downloads/python") as downloader:
            for card in notes:
                note = await reader.get_note(card.url)
                result = await downloader.download_note(note)
                print(note.title, result["status"])

asyncio.run(main())
```

## 原有自动发布

```bash
pip install -r requirements.txt
python publish.py login
python publish.py image --title "今天的晚霞" --content "记录生活" --images /path/to/cover.jpg
```

新搜索/下载模块不依赖原有逆向协议。历史接口与发布使用方式见 [历史模块文档](docs/legacy-session.md)。

## 开发与测试

```bash
pip install -r requirements-reader.txt ruff
python -m unittest discover -s tests -v

# 真实浏览器运行本地模拟页面；测试不访问小红书，不需要登录
python -m playwright install chromium
REDCRACK_BROWSER_TESTS=1 python -m unittest discover -s tests -v

# 本机使用 Chrome 时
REDCRACK_BROWSER_TESTS=1 REDCRACK_TEST_BROWSER=chrome python -m unittest discover -s tests -v

ruff check xhs.py request/reader tests/test_reader*.py
ruff format --check xhs.py request/reader tests/test_reader*.py
```

GitHub Actions 执行离线单元测试、HTTP 下载集成测试及 Chromium 网页集成测试。测试 fixture 均为合成数据，不包含真实账号或登录凭据。测试边界及架构见 [开发说明](docs/reader-design.md)。

## 许可证

保留项目现有专有许可，详见 [LICENSE](LICENSE)。
