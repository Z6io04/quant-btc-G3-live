# G3 日常运维与排查

以下命令在**服务器 SSH 终端**、原部署普通用户下执行。已有运行安装保留 `$HOME/btc-G3-live` 的原始配置、凭据、账本和账户登记。不要把最新源码 zip 直接覆盖进去，也不要重做首次初始化。

## 最常用的检查

```bash
python3 "$HOME/btc-G3-live/launch.py" status
systemctl --user is-active btc-G3-live.service
journalctl --user -u btc-G3-live.service -n 80 --no-pager
```

`status` 展示账本最近核对的资金和持仓，并额外访问公开时间接口；它不会读取私有账户或下单。看“最近运行检查”“最近核对”时间是否推进，结合阶段、未核对订单数和交易所实际账户判断，不只看 `active` 或“没有登记完整组合”。执行进度未完成时可能暂时只有一腿。

网络慢时可以先看本地记录：

```bash
python3 "$HOME/btc-G3-live/launch.py" status --local
python3 "$HOME/btc-G3-live/launch.py" status --local --json
```

`--local` 的输出可能陈旧，不能当作交易所实时持仓证明。只读诊断另外执行：

```bash
python3 "$HOME/btc-G3-live/launch.py" check
python3 "$HOME/btc-G3-live/launch.py" account-check
```

`check` 无 API 密钥也可探测公开数据；`account-check` 读取账户、设置、费率和资金计划，都不下单。正常持仓期间资金计划可能不适合再开仓，不要因为计划不允许就反复重新启用。账户读取成功也不是“交易权限已实单验证”。

## 暂停、恢复与退出

**暂停新增交易**，保留运行中的服务管理已有仓位：

```bash
python3 "$HOME/btc-G3-live/launch.py" pause
python3 "$HOME/btc-G3-live/launch.py" status
```

**恢复入场条件检查**：

```bash
python3 "$HOME/btc-G3-live/launch.py" resume
python3 "$HOME/btc-G3-live/launch.py" status
```

两个命令均把控制请求放入账本队列，服务下一轮处理。命令返回不代表已经执行；若服务停了或 API 无法访问，请求可能仍排队。`resume` 不清除风控停止、身份冲突或账户差异。

**确实要退出组合时**：

```bash
python3 "$HOME/btc-G3-live/launch.py" flatten
```

工具要求输入 `FLATTEN G3`，然后请求真实买平币本位空头、卖出释放的 BTC 并暂停新增交易。保持交易服务运行，检查 `status`、未核对订单和欧易实际持仓/委托，确认退出完成。`flatten` 不是即时平仓保证，网络或交易所问题可能使请求无法执行。

以下命令仅停止进程，**不会平仓**：

```bash
systemctl --user stop btc-G3-live.service
```

如果已核实无需继续后台运行，要同时取消重启自启：

```bash
systemctl --user disable --now btc-G3-live.service
```

维护完成恢复原账本运行用：

```bash
systemctl --user start btc-G3-live.service
python3 "$HOME/btc-G3-live/launch.py" status
```

已经启用的安装恢复运行不用再 `arm-live`。存在未知订单、仅成交一腿或余额差异时，保留订单编号和账本核对；不要通过删数据库、重新下单或随手手工交易来“修正”显示。

## 资金、密钥和代码维护

- **追加资金**：按 [追加资金流程](ADD-CAPITAL.md) 先核对完全空仓、无 BTC 零头/未核对订单，停服后手动转入交易账户，再 `sync-capital`。同步需要确认 `SYNC G3`，不转账、不下单、不提高累计亏损限额；充值不计为收益。
- **配置和核心代码**：首次启用时会记录指纹，直接改 `launch.py`、`g3core/` 或 `config.json` 可能让身份检查失败。本交付核心保持原样，不需要覆盖现有安装来获得文档。
- **密钥**：`setup-credentials` 是首次设置工具，拒绝覆盖。已启用后更换 API 是专门维护操作，不能简单删除凭据重填，因为 API 身份与原账本绑定。
- **账本**：`var/live/`、账户登记、`private/` 和代理私有状态都需要保留在可信本地备份中；备份含秘密，不传 GitHub。不要把正在写入的 SQLite 文件当成完整可靠备份直接复制，正式备份应安排维护停服并保留相关文件。
- **旧 F3**：只在尚未启用 G3 的受控迁移中使用 [F3 迁移工具](F3-MIGRATION.md)。已运行 G3 不再处理旧登记。

## API/预检失败时按层排查

先区分以下四层：代理进程活着、公开网络可用、账户可以读取、真实交易循环在持续核对。任何一层通过都不自动证明其余层通过。

| 现象 | 检查与处理 |
|---|---|
| `mihomo-proxy.service` active，公开接口失败 | 进程还在但节点/路由可能失败；用看守 `check` 查看当前线路，用独立候选探测检查其他节点 |
| HTTP 401 / 签名或白名单错误 | 运行下面的只读诊断，按脱敏错误码核对实盘 API、Passphrase、权限、服务器时间和实际 OKX 出口；不要发真实订单测试 |
| ipify 返回地址已允许，OKX 仍拒绝 IP | ipify 与 OKX 可能走不同 IPv4/IPv6 出口；以 OKX 明确报告/可核实的实际出口为准，在欧易手动更新相应 API 白名单 |
| 公共时间能读，`check` 仍失败 | `check` 还查行情、价差、产品规格、时间新鲜度和资金费历史；一次网络成功不能代替整套预检 |
| `account-check` 报账户/杠杆错误 | 核对合约模式、净持仓模式、**BTC-USD-SWAP 全仓 2 倍**；程序不替你改交易所设置 |
| 报 F3/原独占登记 | 保留登记与旧账本，按受控迁移文档判断资格；不删除文件绕过 |
| 预算计划不允许 | 查看计划理由，当前最小数量、取整、费用和资金可能不满足；不把它当成代理故障 |
| `funding_signal.allowed: false` | 通常表示本轮不符合入场规则，服务可正常等待；不用重启催单 |
| `needs_attention` / `halted` | 查看最近事件、未知订单、资金或仓位差异，核对交易所；`resume` 不会清掉这些保护 |
| 服务 failed / 持续重启 | 先看日志；缺配置/密钥、未启用、目录错误、文件指纹变化或运行锁冲突均须处理原因 |

包内的诊断工具不下单，只查询公开时间和只读账户配置：

```bash
cd "$HOME/btc-G3-live"
python3 -B tools/diagnose_account.py
python3 -B tools/diagnose_account.py --json
```

诊断不会打印 API key、Secret、Passphrase、账户 UID 或原始响应全文。不要把完整 `private/credentials.json`、订阅、完整服务环境或包含个人账户信息的 `account-check` 输出公开。无法确定原因时先保留脱敏错误码、时间、服务状态和日志摘要。

## 代理看守维护

完整命令和目录以 [代理配置](PROXY-SETUP.md) 与 [看守说明](../proxy_watch/README.md) 为准。它和 G3 是两个独立进程，停止看守不会停止 Mihomo，也不会自动停止 G3。

```bash
systemctl --user is-active mihomo-proxy.service proxy-watch.service
journalctl --user -u proxy-watch.service -n 80 --no-pager
```

看守当前线路 `check` 只检查 17890，不扫描、不换线。候选 `probe_nodes.py` 要求先停止看守，在临时 17891 验证，不改变正式配置或重启生产服务；完成后恢复之前运行的看守。`once --apply` 是有状态、可能切换的操作；默认连续失败 3 次才全扫。若后台看守已经 active，让它管理失败计数，不同时运行另一份 `once --apply` 或 `run`。

修改正式配置、订阅或手工换节点前，先停止看守，确认它已退出，再按代理文档维护：

```bash
systemctl --user stop proxy-watch.service
systemctl --user is-active proxy-watch.service
```

`inactive` 表示看守停止，生产 Mihomo 仍由自己的服务管理。维护完成并通过当前线路与 G3 只读检查后，再按原配置启动看守。切换共用 17890 会影响所有使用它的策略；不要在持仓时故意断开代理来制造验收故障。

## SSH 断开和重启

```bash
systemctl --user is-enabled btc-G3-live.service mihomo-proxy.service
loginctl show-user "$(id -un)" -p Linger
```

用户服务 `enabled`、linger 为 `yes` 时，关闭 SSH 不应停止后台服务。重新登录看检查/核对时间是否更新。没有必要为了新打包而重启服务器；计划重启会中断同机其他程序，应放在完成账户核对的维护窗口。重启恢复使用原账本，不重复启用。详见 [验收清单](VERIFY-AFTER-INSTALL.md)。
