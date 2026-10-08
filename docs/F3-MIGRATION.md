# 仅供未来未启用G3时退役旧F3登记

**当前用户的旧登记已经归档，G3已用30U启用运行。当前账户不要运行本工具，不要再次arm，不要删除任何登记。** 本页只说明将来遇到同类残留登记时的受控处理方式。

删除 `~/btc-F3-live` 目录不会删除账户独占登记。`tools/retire_f3_binding.py` 只支持核对并退役当前API账户的精确两份旧F3登记：

- `~/.local/state/btc-F3-live/accounts/<账户哈希>.json`
- `~/.local/state/btc-v53-live/accounts/<同一账户哈希>.json`

两份文件必须都是 `F3-live-1`、同一UID和binding_id、原目录严格为 `~/btc-F3-live/var/live`；共享登记的账户哈希、真钱模式、active阶段及字段必须完全一致。缺少一份、身份不同、其他策略的登记都拒绝处理。工具不修改其他账户、任何交易账本、代码、旧目录或API权限。

## 使用前提

仅Linux普通用户运行，不用sudo。当前G3必须从未启用、没有账户绑定或任何账本记录；已armed的G3一定拒绝，暂停或停服不会解除该限制。若旧F3仍持仓、有未知订单、需要保存跨版本收益/峰值/冷却，应先完成专门的交易恢复与账本迁移；本工具没有这类能力，也不能证明历史盈亏已经结清。

先人工关闭旧F3自动启动并停止G3，例如用户服务：

```bash
systemctl --user disable --now btc-F3-live.service
systemctl --user stop btc-G3-live.service
```

若以前安装过系统级同名服务，需由管理员停止/禁用对应系统服务；不存在的服务不必新建。工具会只读检查用户与系统级F3/G3共四个服务的LoadState、ActiveState、SubState和MainPID。运行、启动中、停止中、重载中、非零PID、无法读取systemd状态均拒绝；不扫描 `/proc`，不要求进程扫描权限。也必须停止手工启动的交易进程：工具持有原共享账户flock和G3本地锁，不能取得锁就拒绝。

应先在G3目录准备正确的本地配置和该账户API凭据。通过设置好的代理读取私有账户；客户端明确 `orders=False`。每次检查要求当前账户无任何持仓、普通委托或算法委托，BTC现金/权益/可用余额明确为零。字段缺失、负余额、网络失败或身份变化都不视为通过；未来检查不假定USDT一定是30U。

## 默认只读检查

```bash
cd ~/btc-G3-live
python3 -B tools/retire_f3_binding.py
```

默认不创建备份、锁文件、账本或新登记，也不移除文件。它可取得已存在的锁并作只读API和systemd核对；不存在的锁不被创建。输出不包含UID、API密钥或原始异常响应。检查通过不会自动执行退役。

## 明确退役

仅在以上条件都满足、明确准备将此空仓账户作为新的G3启用账户时执行：

```bash
python3 -B tools/retire_f3_binding.py --apply
```

工具重新核对停服、未启用状态、账户身份、空仓与余额，先以0600权限完整备份两份原始字节至 `private/f3-binding-retirements/<随机标识>/`（目录0700）。两份备份均落盘后，再次核对账户、配置/凭据、文件内容与inode，最后仅移除这两份旧登记。其间出错会恢复已移除的原登记；遇到并发新内容不覆盖，保留备份和audit并要求人工恢复。磁盘/恢复失败时不可继续启动交易。

备份和audit为私有文件，包含原账户登记信息；不要提交GitHub。`audit.json` 的 `retired` 表示两份退役完成；`rolled_back` 表示失败后原记录恢复；`backing_up`/`prepared`/`manual_recovery_required` 均需要核对现场，不要删audit或绑定强行重试。进程被强杀或断电时不能保证立即自动回滚；先停止全部交易、核对两份备份和当前登记，再人工恢复完整一致状态。

工具不会执行arm、run、enable、start、转账或订单。成功只代表旧独占登记已退役；后续新的G3启用流程须由用户另行执行。**当前已运行的30U G3不适用这些步骤。**

离线测试：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/tests -v`。全部API及systemd为模拟，临时文件隔离，不接触真实服务器或账户。
