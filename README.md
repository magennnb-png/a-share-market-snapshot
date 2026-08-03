# A股实时行情与分时数据桥

一个可在 Windows 上定时运行的公开行情采集项目。程序优先访问东方财富，失败时用腾讯补齐实时行情和 1 分钟分时，并用新浪补齐全市场宽度和行业涨跌榜。任何单一接口失败都只会记录错误并触发回退，不会用新闻稿替代行情。

## 输出

- `data/latest.json`：主要指数、全市场宽度、行业前后十、watchlist 实时行情。
- `data/latest.md`：实时行情的人类可读版本。
- `data/latest_intraday.json`：从当天开盘以来的完整 1 分钟数据及路径指标。
- `data/latest_intraday.md`：分时摘要和完整分钟表。

所有输出都包含行情时间、生成时间、数据来源、过期标志、错误和警告。指数的 `vwap` 是按指数分钟成交量加权的指数点位，明确不是 ETF 交易价格、IOPV 或基金净值。

## 安装

要求 Windows 和 Python 3.11 以上。

```powershell
cd "C:\Users\李森淼\Documents\金融数据拉取"
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

中文 Windows 用户目录如导致 `venv` 启动异常，可先为 Python 建立纯英文目录联接，再重建虚拟环境；本机已使用 `C:\Users\Public\Python312` 完成兼容处理。

## 配置 Watchlist

编辑 `config/watchlist.yaml`。每个标的同时声明东方财富 `secid` 和腾讯代码：

```yaml
- name: 贵州茅台
  symbol: "600519"
  kind: stock
  eastmoney: "1.600519"
  tencent: sh600519
```

`kind` 可用 `index`、`etf`、`stock`。项目只接受公开行情配置，不要写入券商账号、身份证、持仓、GitHub Token 或密码。

## 运行

只生成文件：

```powershell
.\.venv\Scripts\python.exe -m a_share_bridge.main
```

生成后提交并推送四个输出文件：

```powershell
.\.venv\Scripts\python.exe -m a_share_bridge.main --publish
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Windows 任务计划

以当前用户打开 PowerShell，执行：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

脚本创建工作日 08:58、10:58、13:58 三个任务。`run_snapshot.ps1` 还会调用中国节假日判断；非交易日直接退出，交易日自动采集、提交并推送 `main`。

移除任务：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\uninstall_tasks.ps1
```

## 数据口径

- 实时指数与 watchlist：东方财富；失败时腾讯。
- 完整 1 分钟分时：东方财富；失败时腾讯。
- 全市场成交额、上涨/下跌/平盘：东方财富；失败时新浪 `hs_a` 分页行情。
- 涨跌停数：按 ST 5%、主板 10%、创业板/科创板 20%、北交所 30%，使用 0.2 个百分点容差近似计算。停牌、无成交和特殊上市状态可能造成少量口径偏差，输出中会保留警告。
- 行业前后十：东方财富行业板块；失败时新浪行业板块。
- 过期：交易时段内行情时间落后生成时间超过 5 分钟，或行情日期不是当天。

公开接口可能调整字段、限流或暂时阻断。`source_status`、`errors` 和 `warnings` 用于审计每次运行实际使用的来源与降级情况。
