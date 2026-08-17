# ChatGPT 研究上下文

一键更新会在实时行情、滚动历史和校验完成后，自动生成四个轻量 JSON。用户仍只需双击 `update_market.bat`；不需要手工选择 SQLite/CSV，也不需要单独运行导出命令。

## 文件

- `data/research_context/market_technical.json`：7个核心指数最近120个交易日日K、最多52根周K、MA5/10/20/60、5/10/20/60日收益、阶段高低点、距离和振幅。
- `data/research_context/rotation_context.json`：当前行业排名、相对强弱、扩散/成交变化、轮动状态及最近10个交易日同口径历史。字段不可可靠计算时为 `null`，并在 `null_reasons` 中说明原因。
- `data/research_context/market_breadth_context.json`：上涨/下跌/平盘、近似涨跌停、全市场成交额、市场涨跌幅中位数和实际观察到的近期历史。
- `data/research_context/watchlist_context.json`：观察池当前行情、最近120个交易日日K、技术结构和当日1分钟分时摘要。

所有文件都有 `generated_at`、`data_as_of`/`market_time`、`backend`、`sources`、`quality` 和 `known_limitations`。ChatGPT 应先读四个 context 文件，再按需要读取 `data/latest*.json` 或 `data/history/*.csv` 复核。

## SQLite 与 CSV

统一 History Provider 会优先使用本机 `data/history/market_history.db` 中至少20日、且日期不落后于CSV的序列；否则自动降级到 GitHub 可读的 `indices_daily.csv` / `watchlist_daily.csv`。两种后端输出完全相同的 JSON 字段，并在标的级 `backend` 中披露实际选择。

轮动同理：本机 `rotation.db` 可用时输出5分钟快照推导的15/30/60分钟相对强弱、扩散度变化、成交占比变化、领涨股持续性和 rotation score/state；数据库不存在时使用 `industries_daily.csv` 日频降级，无法计算的盘中字段保持 `null`。

## 成交额口径

`amount` 只保存数据源直接返回、且能确认单位的成交额。统一单位为 `CNY`，并保留 `amount_source`、`amount_unit` 和 `amount_coverage_ratio`。没有可靠成交额时保存 `null`，不会使用成交量乘收盘价或均价估算。周成交额仅在该周全部日K都有可靠人民币成交额时汇总。

## 同花顺免费数据实测（2026-08-17）

使用 AKShare 1.18.91 对 `stock_board_industry_name_ths` 和 `stock_board_concept_name_ths` 各连续请求3次，6次均在 `q.10jqka.com.cn` 发生 TLS/SSL EOF；直接请求公开行业、概念页面和 `d.10jqka.com.cn` 日线地址也重复失败。公开网页无需登录且搜索引擎可读取，但当前本机网络路径不能把它视为稳定的一键生产数据源。

浏览器可读的[同花顺概念目录](https://q.10jqka.com.cn/gn/)能够识别“存储芯片”“芯片概念/第三代半导体”“共封装光学(CPO)”“PCB概念”“人形机器人”“液冷服务器”“AI应用”和“创新药”，详情页也展示成分股排行榜；目录中没有完全同名的“AI服务器”，更接近“数据中心(AIDC)”“算力租赁”等口径。这个结果只证明网页上存在这些分类，不代表本机程序已经能稳定自动采集目录、日K和完整分页成分股。

因此本轮没有把 AKShare 加入核心依赖，也没有替换东方财富/新浪行业流程。概念板块目录、日K、成分股在本机自动化环境中均未达到可用标准。后续若网络条件变化，应重新做多次请求、字段结构、分页成分股和限频测试，再作为 warning-only 增强源接入。

同花顺 iFinD Quant API 是官方账号型接口。[官方免费版权限页](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/permission.html)列出了免费额度，但仍需 iFinD 账号登录、令牌配置，并受月度数据量、历史年限和单次提取限制；它比网页抓取更适合稳定生产，但不是当前项目的免配置公共接口。本轮未正式接入。

## 研究措辞

轮动结论只能描述价格、成交、扩散度、相对强弱和持续性。没有可验证的真实资金数据时，不得把结果表述为“主力资金流入”或“机构进场”。
