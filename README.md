# A股行情快照与历史数据库

这是一个完全在本地手动运行的 A 股行情采集工具。Windows 用户双击 `update_market.bat` 即可依次执行：

`git pull --ff-only → 获取真实行情 → 校验 → 增量更新历史 → commit → push`

它不依赖 Codex、OpenAI API 或 GitHub Actions，也不会在代码中保存 GitHub 密码、Token 或 SSH 私钥。

## 首次使用

需要 Windows、Git for Windows、Python 3.11+，并先让本机 Git 能正常访问仓库。

```powershell
git clone https://github.com/magennnb-png/a-share-market-snapshot.git
cd a-share-market-snapshot
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
git config --global credential.helper manager
git push --dry-run
```

第一次 `git push` 可能弹出浏览器登录 GitHub。Git Credential Manager 保存凭据后，日常不需要重复登录。也可继续使用本机已配置好的 SSH remote。

每次行情运行都会主动重取指数/ETF 滚动窗口；行业窗口在发现过期或缺口时自动完整重建。因此即使数周没运行也会自动补齐。行业重建根据网络情况约需 1–3 分钟，同日重复运行会复用已校验窗口。

## 日常使用

双击仓库根目录的 `update_market.bat`，等待窗口显示 `Git Push: SUCCESS`。脚本失败时不会提交坏数据，窗口会停留并显示网络、数据源、过期行情、JSON、Git pull/commit/push 等具体步骤。

GitHub 网络暂时不可用时，行情抓取、校验和本地 commit 仍会继续。窗口会显示 `Market update: SUCCESS` 和 `Git push: FAILED - NETWORK`，本地提交会保留；下次运行会自动先尝试上传旧提交。只有明确检测到 non-fast-forward、分叉历史或 merge conflict 才停止并要求人工处理，脚本不会自动 merge、reset 或 force push。

PowerShell 等价入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\update_market.ps1
```

测试模式（不 pull、commit 或 push）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\update_market.ps1 -SkipPull -NoGit -NoPause
```

## 输出文件

- `data/latest.json`：核心指数、全市场宽度、全量行业、watchlist 当前快照。
- `data/latest_intraday.json`：当日完整分时及 VWAP 等指标。
- `data/latest_rotation.json` / `.md`：最新行业轮动摘要。
- `data/research_context/market_technical.json`：7个核心指数的日K、周K与技术结构。
- `data/research_context/rotation_context.json`：SQLite 5分钟轮动优先、CSV日频降级的统一轮动接口。
- `data/research_context/market_breadth_context.json`：当前及近期实际观察到的市场宽度。
- `data/research_context/watchlist_context.json`：观察池日K技术结构、当前行情与分时摘要。
- `data/history/indices_daily.csv`：7 个核心指数最近 500 个交易日 OHLCVA，每次重取滚动窗口。
- `data/history/watchlist_daily.csv`：芯片产业 H30007、中证传媒 399971、中证创新药产业 931152，以及 watchlist 中的指数/ETF；每次重取滚动窗口，可在 `config/watchlist.yaml` 增删。
- `data/history/market_breadth_daily.csv`：每次实际运行观察到的涨跌/平盘、涨跌停、全市场成交额；同日覆盖、跨日追加。
- `data/history/industries_daily.csv`：最近约 200 个交易日行业涨幅、成交额、活跃度和排名，每次重建滚动窗口。
- `data/history/rotation_daily.csv`：每日前三/后三行业。
- `data/history/intraday/YYYY-MM-DD.json`：最近 10 个交易日分时，自动滚动删除更早文件。

CSV 使用 UTF-8 BOM，便于 GitHub、ChatGPT 和 Windows Excel 读取。`market_time` 是行情本身时间；`generated_at` 是本次文件生成时间。

四个 research context 也由同一个一键入口自动生成。它们优先读取本机 SQLite，数据不可用或落后时自动使用 GitHub CSV，用户无需手工选择。详细字段、成交额口径和同花顺免费数据实测见 [`docs/research-context.md`](docs/research-context.md)。

## 数据源与降级

- 实时指数/watchlist/分时：东方财富，失败时腾讯。
- 当前市场宽度和行业：东方财富，失败时新浪。
- 指数历史：中证指数官网、东方财富、腾讯、新浪、搜狐按可用性降级。
- 市场宽度历史：公开免费接口无法可靠还原过去某日的涨跌家数，因此不伪造漏跑日期，只保存每次实际运行快照；涨跌停按 ST 5%、主板 10%、创业板/科创板 20%、北交所 30% 近似统计。
- 行业历史：优先东方财富行业指数；不可用时用新浪当前行业成分和腾讯个股日线做等权涨跌聚合。`source` 字段明确记录方法，避免把聚合结果误当成官方行业指数。

单一数据源失败会自动降级。若核心指数不全、交易时段行情过期、宽度样本不足、历史天数/OHLC/重复键不合格，更新会失败，临时数据不会覆盖正式 `data/`。

## 桌面快捷方式

右键 `update_market.bat` → “显示更多选项” → “发送到” → “桌面快捷方式”，然后将快捷方式改名为 `更新A股行情`。不要把 BAT 单独复制到桌面；快捷方式应指向仓库里的 BAT。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe update_market.py --validate-only
```

现有任务计划脚本和报告生成器仍保留，但本地一键更新完全不依赖它们。
