# 可选代理看守（默认关闭）

这是独立工具，G3 不依赖它。包内只有源码、示例和离线测试；未读取真实订阅或密钥，也未替换服务器配置、运行代理、启动服务或下单。它只访问公开 HTTPS 接口，不接收交易 API key，不修改 API 权限或 IP 白名单，不发送通知。

**默认 `check` 只读。`allowed_ips: []` 时，即使使用 `run`，也不会自动切换。支持本地订阅中受支持 schema 的直接 VLESS/TLS（含 ECH）和 AnyTLS 节点。**

从没有代理的新服务器开始，先按 [完整代理安装指南](../docs/PROXY-SETUP.md) 安装官方 Mihomo、私下传入订阅、原生校验候选并初始化服务。已有代理保留生产文件。下载/配置工具位于 `tools/mihomo/`，不依赖旧配置脚本。

## 行为

- 每 60 秒通过 `http://127.0.0.1:17890` 实际访问 `https://openapi.okx.com/api/v5/public/time`，要求 HTTP 200、业务 `code="0"`、有效毫秒 `ts` 且与本机时差不超过 120 秒。本机应有正常的时间同步。不会以 `www.okx.com` 成功替代实际 `openapi.okx.com` 失败。若其他程序确实使用 `www.okx.com`，可显式修改 `okx_time_url`；仅允许这两个官方 origin。
- 连续 3 次失败才扫描候选。每个候选在独立目录、独立 Mihomo 进程、独立本机端口 17891 测试。先运行安装版本的 `mihomo -t` 验证完整配置，再验证 OKX 时间接口及 HTTPS 公网出口 IP。临时候选不会接管 17890。扫描期间后台线程继续每 60 秒探测原 17890；发现它恢复则取消切换。
- 候选出口 IP 必须精确命中用户显式填写的 `allowed_ips`。公网 IP 查询默认 `https://api.ipify.org?format=json`，不发送 keys。**公网 IP 证据不证明 OKX 私有 API 权限可用，也不能保证 OKX 看到的出口完全相同。G3 必须每轮自行核对私有权限和账户状态后才交易。** 若出口随目标域名变化，这个看守不能提供同出口保证，不应据此开放自动交易。
- 候选测试结束后关闭自己的临时进程、释放 17891，再备份正式配置并原子替换、重启 `mihomo-proxy.service`。新配置始终绑定本机 17890，控制 API、LAN、TUN 与内置 DNS 均关闭；所有流量 `MATCH` 到选中单节点。正式启动后最多复验 3 次，间隔 2 秒；失败恢复原始配置字节并重启、同样复验。回滚后旧线路已经健康则停止扫描、清除冷却；配置和服务已恢复但旧线路仍不可达时继续余下候选，日志会如实记录。回滚重启失败或发现人工修改时停止改线，等待检查。
- 只有完整扫描无可用节点后，才默认等 3600 秒再全扫，可改 7200；轻量当前线路检查继续。扫描开始前先持久化冷却以防中途崩溃造成重启扫描循环；全部失败后从结束时刻重新计冷却。成功切换或当前线路恢复会清除冷却，此后再次连续 3 次失败即可重新扫描。
- 每次替换保留私有备份和事务 journal。若在替换后崩溃，下一次显式 `run`/`once --apply` 会尝试恢复原配置。若检测到人工配置修改，停止写入并保留 journal，需人工检查；不会覆盖人工版本。

这是连接可用性工具，不能保证任何策略收益，也不会改变 G3 的下单规则。

## 先配置、只读检查

服务器已有 Python 3.9+ 与 PyYAML 时可直接使用；否则可在本目录创建隔离环境（不需要 sudo）：

```bash
cd ~/btc-G3-live/proxy_watch
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp settings.example.yaml settings.yaml
chmod 600 settings.yaml
```

若 Debian 提示无法创建 venv、缺少 `ensurepip`，先执行一次 `sudo apt install python3-venv`，再重新创建 `.venv`。这是安装 Python 的系统组件；看守本身仍以普通用户运行。pip 需要能连接软件源，安装失败时不要跳过依赖步骤。

编辑 `settings.yaml`，确认路径符合实际用户；`~` 会由 `Settings.load` 展开为运行用户的家目录。订阅必须是事先保存的本地 `~/mihomo-subscription.yaml`；看守本身不下载或执行订阅，不执行订阅里的脚本、规则或 provider。

`allowed_ips` 填写经你核实的固定 ipify 出口 IP，使用 YAML 字符串列表；另行核对并登记 OKX 实际出口，不能假定二者相同。不要直接照抄公共示例地址；也不要为了通过检查让程序自动扩大交易 API 白名单。空列表保持自动切换关闭。多个节点允许使用同一个已批准出口 IP。

```bash
.venv/bin/python -B proxy_watch.py --config settings.yaml
.venv/bin/python -B proxy_watch.py validate --config settings.yaml
```

第一条等同 `check`：只做公开网络探测，不创建状态目录，不读取订阅，不重启服务或修改文件。IP 集合非空时也会检测当前出口 IP。第二条只读校验本地订阅与正式配置结构，逐个报告被拒绝节点的序号与原因；序号从 0 开始。任何拒绝都会返回退出码 2。候选和正式配置的完整 Mihomo 原生 `-t` 验证还会在实际使用前执行。

支持 TCP、WS、gRPC、H2、HTTP 的直接 VLESS/TLS 节点及 schema 中列出的 ECH、Reality、指纹、传输等字段；支持 AnyTLS 的密码、SNI、ECH 和受支持会话参数。被支持节点的原字段/值完整保留，不擅自删字段使其“能用”。未知类型、未知字段、XHTTP、`dialer-proxy`、provider/代理组引用、TLS 验证关闭、YAML alias/重复键/对象标签均明确拒绝；运行模式把拒绝原因记入本地日志，只测试剩余合格节点。若你的订阅使用这些功能，应扩展并测试 schema，或继续手动管理，不要删除依赖字段强行通过。顶层订阅中的 rules、groups、providers 等不会被继承或执行。正式配置也必须是单个直接 VLESS 或 AnyTLS + 单条 MATCH 的受支持简单配置。

## 独立候选诊断

先停止看守（前台用 Ctrl+C，后台用 `systemctl --user stop proxy-watch.service`），再运行：

```bash
.venv/bin/python -B probe_nodes.py --config settings.yaml
```

`--nodes` 可只测试本次订阅的原始编号，例如 `--nodes "$NODE"`；编号从 **1** 开始，而 `validate` 报告的 `index` 从 **0** 开始，不能混用。用仓库根目录下的 `proxy_watch/.venv/bin/python tools/mihomo/configure.py --list` 获取本次支持编号，不固定任何旧订阅序号。

`healthy` 表示公开 OKX 与 ipify 都成功；`partial` 表示公开 OKX 已成功而 ipify 失败；`failed` 表示候选启动或公开 OKX 失败。结束的 `healthy_nodes` 和 `okx_reachable_nodes` 分别计数，不能把 partial 都说成“OKX 不通”。这条命令不切正式节点、不写 `allowed_ips`，输出的 ipify IP 也不是 OKX 私有权限通过证据。它忽略白名单做观察，需要看守停止，候选运行结束自动清理。

`validate` 遇到任何不支持的节点即退出 2，混合订阅中其它节点仍可能合格。因此把结构校验与候选测试分开执行，不能用 `validate && probe_nodes.py` 串联后断言“所有节点测试失败”。

**ipify 的 IPv4 可能与 OKX 实际观察到的 IPv6 不同；即使 Mihomo 全局 `ipv6: false`，也不能约束远端代理对 OKX 的出口。** 对每条候选，人工登记每个共用代理 API key 的实际 OKX 出口白名单，并完成各策略的只读账户/权限检查。看守不做私有 API 验证，不能将自动切换宣称为已验证私有交易访问。

## 显式启用

一次有状态检查（有连续失败状态且满足条件时**可能改线**）：

```bash
.venv/bin/python -B proxy_watch.py once --apply --config settings.yaml
```

持续自动运行（这个命令本身就是启用自动改线的明确操作）：

```bash
.venv/bin/python -B proxy_watch.py run --config settings.yaml
```

`proxy-watch.service.example` 仅是模板，没有自动安装/enable/start 脚本。需要后台运行时，确认模板部署目录正确、上面的依赖和配置检查已完成，再执行：

```bash
mkdir -p "$HOME/.config/systemd/user"
ls -l "$HOME/.config/systemd/user/proxy-watch.service"
```

只有提示文件不存在时才复制；已有同名文件时先保留并核对：

```bash
cp "$HOME/btc-G3-live/proxy_watch/proxy-watch.service.example" "$HOME/.config/systemd/user/proxy-watch.service"
systemctl --user daemon-reload
```

上面只安装模板。**下面一步由你开启长期自动改线**，共用代理的 v53/F3/G3 都会受到影响：

```bash
systemctl --user enable --now proxy-watch.service
systemctl --user is-active proxy-watch.service
journalctl --user -u proxy-watch.service -n 50 --no-pager
```

查看本地检测记录：

```bash
cat "$HOME/mihomo/private/proxy-watch/state.json"
tail -n 20 "$HOME/mihomo/private/proxy-watch/events.jsonl"
```

没有出现过异常事件时 `events.jsonl` 可能尚不存在。停止自动改线并取消看守的开机启动用 `systemctl --user disable --now proxy-watch.service`；这条命令不停止 Mihomo 或量化程序。示例服务不会创建依赖环境、写入白名单或启动交易程序。

## 对共用 v53/F3 的影响及手工维护

G3、v53、F3 只要共用 127.0.0.1:17890，都会随该端口一起换出口。重启 Mihomo 会短暂断开所有这些程序的 HTTP/长连接，可能导致请求超时；各程序应先核对交易所实际订单/持仓，再决定是否重试。不要在其他程序仍会盲目重试下单时启用自动切换。

手工改线、更新正式 config 或订阅前，**先停止看守**（后台运行用 `systemctl --user stop proxy-watch.service`，前台运行用 Ctrl+C）。看守的单实例锁是合作式锁，普通配置脚本未参与该锁；hash + mtime + inode 复核能发现多数并发修改，但不是针对不合作写入者的绝对原子 CAS。停止看守并确认它已退出后，才使用本包候选工具并按维护流程修改配置；维护完成再启动看守。不要重跑旧版更新/配置脚本，以免恢复已经修复的根 URL、VLESS/ECH 或 AnyTLS 缺陷。

状态目录默认 `~/mihomo/private/proxy-watch`，必须由当前用户拥有且权限 `0700`。`state.json` 保存失败数及下次全扫时间；`events.jsonl`/`events.previous.jsonl` 是滚动本地事件日志；`backup-*.yaml` 含订阅凭据，文件权限 `0600`，不要上传或公开。日志不记录 UUID、节点完整配置、密钥或响应体。备份不会自动删除，可在确认稳定且无 pending journal 后自行按保留策略清理。

出现 `transaction.json` 且 `blocked_pending_transaction`/配置冲突时，停止看守、对照备份和当前配置检查真实服务状态。不要直接删 journal 强行继续；先确认并恢复需要保留的配置、验证 17890，再决定处理 journal 与重启看守。候选端口占用时只报告失败，绝不会杀掉占端口的其他程序。

## 离线测试

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

测试只用合成节点、模拟 HTTPS/服务重启，以及一个自己创建并清理的临时本机监听进程；不连接公网、不读取真实订阅、不接触正式 17890 服务。需要测试环境允许绑定本机临时端口。覆盖业务码/时间戳/IP门禁、显式代理与TLS、三连败、跨重启冷却、恢复、回滚、并发修改、单实例锁及候选进程清理。

配置字段依据 [Mihomo VLESS 文档](https://wiki.metacubex.one/config/proxies/vless/)、[AnyTLS 文档](https://wiki.metacubex.one/config/proxies/anytls/) 与 [TLS 文档](https://wiki.metacubex.one/config/proxies/tls/)；实际支持范围以本工具显式 schema 及本机 Mihomo 完整配置校验共同决定。
