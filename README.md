# RedCrack

**小红书自动搜索、批量下载与本地归档工具。** 使用 Python + Playwright，通过独立浏览器保存登录状态，读取网页自己的搜索结果和笔记详情；无需手动填写 Cookie 或维护请求签名。保留原有自动发布模块。

## 功能

- **关键词搜索**：自动滚动翻页、按笔记 ID 去重、保留完整访问链接。
- **批量输入**：笔记 URL、24 位 ID、`xhslink.com` 短链接、分享文案、文本清单、搜索 JSON。
- **图片与视频**：下载详情中的图片及直链视频；视频优先选择 H.264 中分辨率最高的可用版本。
- **本地归档**：正文 Markdown、完整元数据 JSON、可直接用 Excel 打开的 UTF-8 BOM CSV。
- **可重复执行**：流式写入、原子替换、SHA-256 完整性校验；再次运行跳过已完成的相同媒体。
- **失败可追踪**：单素材失败不影响其他素材；每篇下载清单、整批执行记录和明确退出码。
- **登录复用**：首次扫码，之后可使用 `--headless`；登录与下载文件不纳入 Git。

## 快速开始

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
| `--limit` | `20` | 搜索结果数量上限，不保证一定返回足量结果 |
| `--max-scrolls` | `30` | 搜索自动滚动次数上限 |
| `--interval` | `1.5` | 搜索滚动 / 逐篇读取的间隔秒数 |
| `--timeout` | `30` | 页面和结果的等待超时秒数 |
| `--concurrency` | `3` | 单篇笔记的媒体下载并发数 |
| `--retries` | `2` | 网络或媒体服务器临时错误后的重试次数 |
| `--max-size-mb` | `512` | 单个媒体文件的最大大小（MiB） |
| `--profile` | `~/.redcrack/xhs_reader_profile` | 独立浏览器登录目录 |
| `--headless` | 关闭 | 无窗口执行，首次扫码不要使用 |
| `--browser-channel` | `auto` | `auto` / `chrome` / `chromium` / `msedge` |

详细参数：`python xhs.py --help`、`python xhs.py search --help`。

正常结果摘要输出到 stdout，进度和错误输出到 stderr。退出码：`0` 成功，`2` 执行失败或部分下载失败，`3` 需要登录，`4` 需要网页验证，`130` 用户中断。`complete` 表示该次批次已结束，不等于所有媒体都成功；素材成功与否以 `downloads.json` / `batch.json` 的 `status` 为准。

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
