# 部署后验收：区分进程、网络、账户与交易循环

本页以检查为主，不要求重装或重启。已启用运行的 G3 使用原目录与账本检查，不再执行 `setup-config`、`setup-credentials`、`arm-live` 或 F3 迁移。首次部署则在 [完整配置流程](DEPLOYMENT.md) 完成后使用本页。

## 1. 确认使用正确用户和目录

在**服务器 SSH 终端**执行：

```bash
whoami
printf '%s\n' "$HOME"
ls "$HOME/btc-G3-live/launch.py"
```

这是服务器普通用户主目录，不是 Mac 的 `$HOME`。服务使用 `%h/btc-G3-live`，在错误目录中运行另一份源码不能代表生产安装的状态。

## 2. 进程活着与重启自启是两回事

```bash
systemctl --user is-active mihomo-proxy.service btc-G3-live.service
systemctl --user is-enabled mihomo-proxy.service btc-G3-live.service
loginctl show-user "$(id -un)" -p Linger
```

应分别看到需要运行的服务 `active`、需要自启的服务 `enabled`，以及 `Linger=yes`。如果安装了自动看守，再单独检查：

```bash
systemctl --user is-active proxy-watch.service
systemctl --user is-enabled proxy-watch.service
```

看守是可选工具，未配置时不要求它 active。`active` 只表示进程在，不能证明网络健康、API 可读或正在按规则推进。

## 3. 公开数据与账户读取分别检查

```bash
python3 "$HOME/btc-G3-live/launch.py" check
python3 "$HOME/btc-G3-live/launch.py" account-check
```

这两个命令均不下单。公开检查的 `passed` 与 `funding_signal.allowed` 分开看；资金费信号不允许入场，可能是正常策略等待。私有检查的 `private_read_verified: true` 说明该时点账户可读；检查首次入场预算还要看 `entry_budget_plan.allowed`，但已持仓时不能要求它仍允许新入场。

预检还涉及服务器时间、行情新鲜度、价差、账户模式、杠杆和费用。失败时看具体提示，不把所有异常都归为代理故障。读取通过不证明交易 API 权限已实单验证，也不承诺之后每次请求都会成功。不要用真实试单来代替这份验收。

## 4. 交易循环是否持续核对

```bash
python3 "$HOME/btc-G3-live/launch.py" status
journalctl --user -u btc-G3-live.service -n 80 --no-pager
```

过一两分钟再执行一次 `status`，核对：

- 最近运行检查与最近核对时间持续推进，而非只重复旧“空仓”记录。
- 阶段不是持续的 `error` / `needs_attention`，风控停止与暂停状态符合你的预期。
- 未核对订单和余额/持仓差异有明确解释，交易所实际持仓与账本对应。
- 当前预算、累计追加和亏损停止金额符合原启用记录；本次已有安装应核对原 30U/10U，而不是写回交付默认配置。

`status --local` 可帮助网络慢时读取账本，但不能证明实时账户。`status` 的公开网络检查也不执行私有账户读取。不要因网络成功就认定未知订单已核对，不要因没有完整组合就认定不存在单腿风险。

## 5. 自动切换代理的验收

先按 [代理完整配置](PROXY-SETUP.md) 安装依赖并填写自己的 `proxy_watch/settings.yaml`。以下命令都从包根目录执行：

```bash
cd "$HOME/btc-G3-live"
proxy_watch/.venv/bin/python -B proxy_watch/proxy_watch.py check --config proxy_watch/settings.yaml
```

这是**当前生产 17890 的只读检查**，失败时不扫描其他节点、不换线。候选探测要求看守已停止，避免同时占用 17891；这项检查会暂时停止自动看守，但不停止生产 Mihomo 或 G3。仅在准备做候选检查时执行，正常的已有运行验收不必做此维护步骤：

```bash
systemctl --user stop proxy-watch.service
systemctl --user is-active proxy-watch.service
```

确认 `inactive`（新装未创建该 unit 时可不存在），再验证候选：

```bash
proxy_watch/.venv/bin/python -B proxy_watch/probe_nodes.py --config proxy_watch/settings.yaml
```

候选使用独立 Mihomo 进程和临时端口 17891；不接管 17890，不修改正式配置，不重启生产代理。若仅检查指定节点，先列出本次订阅中受支持的节点序号，序号从原始订阅第 1 个节点开始：

```bash
proxy_watch/.venv/bin/python tools/mihomo/configure.py --list
printf '输入上面列表中的节点编号: '
read -r NODE
proxy_watch/.venv/bin/python -B proxy_watch/probe_nodes.py --config proxy_watch/settings.yaml --nodes "$NODE"
```

也可按工具帮助传多个逗号分隔编号；不要沿用别人的订阅编号。`healthy` 仅表示 OKX 公共时间与 ipify 查询都通过；`partial` 可能是 OKX 公共接口可达但 ipify 失败。探测不修改 `allowed_ips`，也不证明自动切换门禁或私有权限通过。配置结构 `validate` 因订阅混有不支持节点可返回退出码 2；读取拒绝原因后可独立探测其余受支持节点，不要用 `validate && probe` 把混合订阅全部挡掉。

**看守已 active 时无需手动触发三次。** 它会自行累积失败。只有在维护窗口、未运行另一份看守、已配置明确允许出口且确实准备允许切换时，才可使用有状态的单次模式：

```bash
proxy_watch/.venv/bin/python -B proxy_watch/proxy_watch.py once --apply --config proxy_watch/settings.yaml
```

默认连续失败阈值为 3。从零失败计数开始，当前生产线路持续失败时，运行三次上述命令才可能开始候选扫描；若中途恢复，会重置失败数，若受冷却/空白允许列表等限制也不会立即切换。不要把它当成“强制换节点”按钮，也不要与后台 `run` 并行执行。`once --apply` 会写看守状态，达到条件时可能替换配置、重启生产 Mihomo，与只读 `check` 不同。

看守的 ipify 出口门禁不等于 OKX 实际出口一致性保证。IPv4/IPv6 或按目的地分流可能让两者不同。欧易白名单须在欧易手动配置；本包不会修改账户白名单。换线后再执行 G3 的 `check`、`account-check`、`status`，并检查所有共用 17890 的其他策略。如果本次只做候选探测，原看守已配置且此前在运行，检查完成后用 `systemctl --user start proxy-watch.service` 恢复它；首次安装是否启用看守仍按代理完整配置文档完成。

## 6. 退出 SSH 与计划重启

正常检查完成后退出 SSH，过一两分钟从 Mac 重新登录：

```bash
ssh YOUR_USER@YOUR_SERVER
python3 "$HOME/btc-G3-live/launch.py" status
```

使用你自己的服务器地址。检查更新时间是否继续推进；终端关闭不表示用户后台服务停止。若要安排专门的退出 SSH 验收，空仓维护时做最容易核对。

不需要为了本次打包验证而重启。确实计划重启时，先安排同机所有策略的维护，核对实际仓位和未知订单；重启会中断共享代理与其他程序。不要在真实持仓或未知订单期间故意重启来制造故障。重启后重复本页第 2—4 步，继续原账本，不重复 `arm-live`，不删除数据库。

## 验收失败时

先记录脱敏错误、时间和状态，按 [运维排查](OPERATIONS.md) 处理。HTTP 401 可用 `python3 -B tools/diagnose_account.py` 分离账户读取错误；未知订单仍须使用原订单编号和交易所真实记录核对。服务自启、网络探测和离线测试都不能保证盈利或消除两腿执行、网络整体不可用、滑点等风险。
