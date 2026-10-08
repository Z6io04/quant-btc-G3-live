# F3 / G3 离线比较

这里完全不读密钥、不调用交易接口、不下真实订单。比较只改变入场评估频率：F3 北京时间 09:00，G3 当前最新完整分钟；两者都在下一分钟重查已结算资金费与当前模拟报价/费用/交易量。

与代码压缩包一起提供的 `G3-backtest-data.zip` 是可选独立数据附件（不包含在源码仓库中）（约 19.5 MB）；解压得到 `g3-backtest-data`。其中所有历史数据及 `manifest.json` 都是旧公开资料的原样副本。manifest 保留全历史来源，比较器只校验这次实际使用的 10 个输入文件，不需要另外下载未使用的三年数据。

Python 3.10+，仅标准库。进入代码包 `btc-G3-live` 目录后运行：

```sh
python3 -m backtesting.compare --data-root /absolute/path/g3-backtest-data --output reports/replayed-results
python3 -m backtesting.audit_results --results reports/replayed-results
python3 -m backtesting.recovery_diagnostic --data-root /absolute/path/g3-backtest-data
```

默认双进程，Linux/macOS 可用 `--workers 1` 降低资源占用。复跑核对 `reports/run-manifest.json` 的源码、设计、输入哈希；公开分发版本已脱敏本地 Python 和数据绝对路径，复跑以源码与输入哈希为准。不要覆盖或改写设计来追求更高收益；要改变参数或窗口应另开有说明的新研究目录。

`reference_f3.py` 是原 F3 生产策略 pure 文件的逐字副本；`compare.py` 直接调用这些 Decimal 规则，启动会比较 G3 的经济函数源码与 RULES。资金费窗口缓存经过 4380 个结算/窗口到期边界的原函数对照。`audit_results.py` 另外从结果逐笔重建 BTC/USDT 账本，检查信号时间、下一分钟重查和离线期间不得入场。

`reports/report.txt` 解释结果与局限。公开蜡烛和小时 mark 不能验证实盘 5 秒内 bid/ask、深度、部分成交、真实资金费账单或分钟强平；这不是实盘成交复现。公开包保留设计修订和最终结果，不附旧中间日志。正式结果仅为 `reports/results/`。
