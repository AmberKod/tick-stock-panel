# yanwei99521/easy-tdx 核实报告（2026-10-12）

## 结论

**和我们 vendor 的是同一个项目**，只是它更新、更胖。

- 地址：https://github.com/yanwei99521/easy-tdx
- 版本 `1.20.8`，Python，23★ / 48 fork，`main` 分支，末次 push `2026-09-01`，非 fork、非归档
- LICENSE：MIT 文本（GitHub 判 NOASSERTION，和 clong365 一样）
- **PyPI 仍然 404**（`https://pypi.org/pypi/easy-tdx/json`）——没发布，只能 clone

### 同源证据（不是猜的）

| 项 | clong365（我们 vendor 的） | yanwei99521 |
|---|---|---|
| NOTICE 全文 | 逐字一致（pytdx / xmtdx 署名段） | 逐字一致 |
| 包名 | `easy_tdx` | `easy_tdx`（`src/easy_tdx`） |
| `ex/commands/` | 12 个文件名完全相同 | 12 个文件名完全相同 |
| `mac/commands/` | 18 个文件名完全相同 | 18 个文件名完全相同 |
| 核心类 | `MacClient` / `ExTdxClient` / `MacExClient` | 同名 |

即：同一血脉。yanwei99521 版 = 在 clong365 2026-05-22 的基线上继续迭代了 3 个月（CHANGELOG 132KB，1.20.x 系列）。

### 它多出来的东西（我们 vendor 里没有）

- `web/`（FastAPI + Vue 前端，`web-ui/`，自带 Swagger、`/api/v1/...`）
- `backtest/` 回测引擎 + `factor/` 因子 + `portfolio/` 组合 + `screen/` 选股扫描 + `strategies/`
- `chanlun/` 缠论、`indicator.py` + `MyTT.py`（34 个指标，含 ZHUOYAO 捉妖大师 / BIAS_SIGNAL）
- `cninfo/` 巨潮、`ths_web.py` 同花顺、`sina/`、`realtime/`
- 连接健壮性：`ex/client.py` 有 `_reconnect`，mac_client 有 heartbeat
- `cli/` 保留（我们 vendor 时删了）

### 🔴 对我们有价值的两个真发现

1. **港股逐笔成交这个坑被它填了**（`src/easy_tdx/ex/_hk_transaction.py`，issue #14）
   - 我们之前探测把「逐笔」判为"EX 不可用"，根因在这：MAC 协议的 `SymbolTransactionCmd` **0x122F** 数据源只覆盖沪深京 A 股 + 部分扩展市场（美股/中金所期货恰好接入），**港股未接入** → 服务器对港股 market 一律返回 39 字节空响应（count=0）。
   - 正确路由：当日 → ex 协议 **0x23FC**（`GetExTransactionDataCmd`）；历史 → **0x2406**（`GetExHistoryTransactionDataCmd`）。
   - 价格字段是整数，**/1000 = 港元**（431400 → 431.4 HKD，与港股分时图 float 价格对齐验证过）。
   - 适用市场集合：27 香港指数 / 31 主板 / 48 创业板 / 49 基金 / 71 港股通 / 98 暗盘（不含期货期权）。
   - → 我们 vendor 的 `ex/mac_client.py` 里 `goods_transaction` 还是无条件走 `SymbolTransactionCmd`，这就是港股逐笔空响应的原因。要补港股逐笔，抄它的路由即可。

2. **QFQ 深层历史负价是上游数据缺陷，不是我们的 bug**
   - 通达信 MAC 服务端在 QFQ 模式下，对 601088 这类长期重度除权股、深层历史页（start 偏移 >~2100）直接返回负价（2013-11-18 QFQ close=-3.80，NONE=16.58 / HFQ=27.17 正常）。
   - 后果链：负 close → 权益为负 → 回撤 >100%、年化 nan、BOLL 在零价 `cash/0` 崩溃。
   - 它在 `mac/commands/symbol_bar.py` + `mac/client.py` + `mac/adjust.py` 做了本地重算兜底。**我们如果后面做复权因子，必须带这层兜底。**

   顺带：`_is_daily_plus()` 查表判定，修掉 `int(cat) >= int(KlineCategory.DAY)` 把 MIN_1(=7) 误判成日线、把 `datetime` 截成 `00:00:00` 的问题（issue #49）。

### 与我们的差异（方法级）

| 能力 | 我们 vendor | 它 |
|---|---|---|
| `goods_transaction` | 无条件 0x122F | 港股自动改走 0x23FC / 0x2406 |
| `goods_transaction_all` | 无 | 有（全量分页） |
| 断线重连 | `_start_heartbeat` | `_reconnect` + 心跳（同步/异步双版） |
| ex/client.py 体量 | 15.3KB | 20.4KB |

## 建议

- 短期不用换源：我们只用在线协议 + 已删 `offline/`，clong365 版够用且已落地 4 块能力 + 136 测试。
- 但要补新维度时，**先来这个仓库翻实现**，别自己探协议：它有 issue 编号、CHANGELOG 根因分析、单测，比我们盲探可靠得多。
- 下一批可捞：①港股逐笔成交（路由已给）②QFQ/HFQ 复权因子 + 负价兜底 ③分钟线分页（700 根上限）。
