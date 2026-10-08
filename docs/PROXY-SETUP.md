# 从零安装 Mihomo 与可选代理看守

下面以 Debian amd64、普通用户、仓库放在 `~/btc-G3-live` 为例。已有 `~/mihomo/bin/mihomo`、`~/mihomo/private/config.yaml` 或 `mihomo-proxy.service` 时，先保留现有文件，跳过对应初始化步骤。本包不覆盖现有正式配置，也不自动启动任何交易策略。

固定接口是 `http://127.0.0.1:17890`；候选测试独占本机 `17891`。Mihomo 服务为 `mihomo-proxy.service`，日志为 `~/mihomo/private/proxy.log`。G3/v53/F3 共用 17890 时，换线和重启会影响所有程序。

## 1. 服务器：依赖与官方二进制

```bash
sudo apt update
sudo apt install python3 python3-venv ca-certificates curl
cd "$HOME/btc-G3-live"
python3 -m venv proxy_watch/.venv
proxy_watch/.venv/bin/python -m pip install -r proxy_watch/requirements.txt
python3 tools/mihomo/install.py
"$HOME/mihomo/bin/mihomo" -v
```

若 `venv` 报 `ensurepip` 不可用，安装 `python3-venv` 后重新执行创建环境和 pip 两步。不要使用系统 pip 强行越过 Debian 的环境保护。

安装器只支持运行于 Linux amd64/x86_64 服务器。它通过 HTTPS 获取 [MetaCubeX/mihomo 官方稳定发行版](https://github.com/MetaCubeX/mihomo/releases/latest)，选取 `linux-amd64-compatible` 归档，核对官方 GitHub API 返回的 SHA256，再解压检查 ELF 架构后安装。缺少摘要、校验不符、下载异常或目标已存在都会停止。校验依赖官方 GitHub HTTPS 元数据，并不是独立签名校验；不会用下载文件自身的哈希冒充可信预期值。实现依据 [GitHub release asset API](https://docs.github.com/en/rest/releases/assets#get-a-release-asset)。版本不写死；若需复现指定稳定版，可先审阅官方发行记录再传 `--tag vX.Y.Z`。

`~/mihomo/bin/mihomo.release.json` 保留版本、资产名及归档/二进制 SHA256。脚本没有升级或强制覆盖选项，不会启动服务；已有安装应先备份并另行安排维护，不要删除在用二进制后盲目重跑。

**服务器无法直连 GitHub 时的备用路径：** 在 Mac 浏览器打开上面的官方稳定发行页，下载该版本的 `mihomo-linux-amd64-compatible-版本.gz`（是 Linux 包），保存为 `~/Downloads/mihomo-bootstrap.gz`。在同一个官方发行页 Assets 中复制该资产的 `sha256` 摘要，保留版本与来源链接；下面的占位文字必须替换为官方提供的 64 位十六进制值，不要用下载文件自己算出的值作预期摘要。

在 **Mac 终端**校验后传输（`your-server` 替换为 SSH 主机别名）：

```bash
cd "$HOME/Downloads"
printf '%s  %s\n' '替换为官方资产的64位SHA256' 'mihomo-bootstrap.gz' > mihomo-bootstrap.SHA256
if shasum -a 256 -c mihomo-bootstrap.SHA256; then
  scp mihomo-bootstrap.gz mihomo-bootstrap.SHA256 your-server:~/
fi
```

在 **服务器终端**再次校验并首次安装。下面在子 shell 中运行，任何失败都会停止；`ln -T` 原子创建目标，已有二进制或符号链接时拒绝覆盖。已有生产安装无需执行本段。

```bash
(
set -eu
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
cd "$HOME"
sha256sum -c mihomo-bootstrap.SHA256
mkdir -p "$HOME/mihomo/bin"
staged="$(mktemp "$HOME/mihomo/bin/.mihomo.XXXXXX")"
trap 'rm -f "$staged"' EXIT
gzip -dc mihomo-bootstrap.gz > "$staged"
chmod 700 "$staged"
ln -T "$staged" "$HOME/mihomo/bin/mihomo"
"$HOME/mihomo/bin/mihomo" -v
)
```

该备用流程的版本/来源记录与 `mihomo-bootstrap.SHA256` 请一并保留。后续仍按下文原生 `-t` 验证配置，再显式启动服务。

## 2. Mac：私下下载订阅，再传到服务器

在 **Mac 本地终端**、仓库根目录执行。下载脚本用隐藏输入读取 URL，URL 不进入命令历史或进程参数；不要把订阅 URL、YAML、UUID/密码上传 GitHub 或粘进公开日志。

```bash
python3 tools/mihomo/download_subscription.py
scp "$HOME/mihomo-subscription.yaml" your-server:~/mihomo-subscription.incoming.yaml
```

`your-server` 替换为你自己的 SSH 主机别名。脚本要求 HTTPS，拒绝覆盖已有本地订阅，默认保存 `~/mihomo-subscription.yaml`、权限 `0600`。如需另存可使用 `--output`；提供给脚本的必须是 Clash/Mihomo YAML 订阅，不能是客户端网页、Base64 节点链接或仅有 provider 的配置。下载成功只表示收到了文件，后续严格解析决定可用性。下载失败不会显示可能含 token 的 HTTP 错误。

回到 **服务器 SSH 终端**，仅在正式订阅不存在时接收它：

```bash
chmod 600 "$HOME/mihomo-subscription.incoming.yaml"
python3 - <<'PY'
from pathlib import Path
import os
incoming = Path.home() / 'mihomo-subscription.incoming.yaml'
live = Path.home() / 'mihomo-subscription.yaml'
os.link(incoming, live)  # 已存在则报错并保留双方文件，不覆盖。
incoming.unlink()
PY
```

若正式订阅已存在，上面的报错是保护措施；保留文件，先按后文维护流程停止看守再审阅变更。后续所有步骤均在服务器运行。无需把 `HTTP_PROXY`/`HTTPS_PROXY` 永久写入 shell 配置；探测工具显式使用本机代理，G3 的代理由它自己的配置决定。

## 3. 服务器：列出节点并独立测试

```bash
cd "$HOME/btc-G3-live"
cp -n proxy_watch/settings.example.yaml proxy_watch/settings.yaml
chmod 600 proxy_watch/settings.yaml
proxy_watch/.venv/bin/python tools/mihomo/configure.py --list
```

列表仅输出原订阅从 **1 开始**的 `node`、协议和拒绝原因，不打印名称/地址/凭据。支持直接 VLESS/TLS（包含受支持 ECH、Reality 与传输字段）和 AnyTLS；字段完整保留，绝不通过删除 ECH、密码或 TLS 字段强行通过。XHTTP、间接依赖、未知 schema、关闭证书验证等仍拒绝。

编辑 `proxy_watch/settings.yaml` 核对路径；默认 `~` 自动指向当前服务用户的家目录。先保留 `allowed_ips: []`，自动换线处于关闭状态。若看守已在运行，先停止它，再检查停止状态；新机器提示 unit 不存在可以忽略：

```bash
systemctl --user stop proxy-watch.service
systemctl --user is-active proxy-watch.service
```

选择 `--list` 给出的一个支持编号，再只测该节点。例如以下命令由你输入编号，不把任何旧订阅的编号当成固定节点：

```bash
read -r -p '输入本次订阅的节点编号: ' NODE
proxy_watch/.venv/bin/python -B proxy_watch/probe_nodes.py --config proxy_watch/settings.yaml --nodes "$NODE"
```

省略 `--nodes` 会扫描本次订阅全部合格节点；也可传逗号分隔编号。探测在独立目录与端口 17891 启动临时 Mihomo，结束后清理，不改 17890 的正式配置。它不需要正式 config 已存在，但需要 systemd 用户会话可用，且看守停止。

- `healthy`：OKX **公开**时间接口与 ipify 查询都成功。
- `partial`：公开 OKX 成功，但 ipify 查询失败；不是“节点完全不可达”。
- `failed`：在候选启动或公开 OKX 阶段失败。
- `unsupported`：schema 不支持该节点，不会删除未知字段再尝试。

`healthy_nodes` 与 `okx_reachable_nodes` 含义不同；整次没有 `healthy` 时退出 1，也可能仍有公开 OKX 可达的 `partial` 节点。完整扫描部分失败不能概括为“全部节点坏了”。输出中的 `observed_ipify_exit_ip` 是需要私下保留的运维信息。

## 4. 首次创建正式配置与服务

当前选中的节点探测合格后，生成候选并用**当前安装的 Mihomo 原生 `-t`** 校验；生成过程不会启动该候选、替换正式配置或重启服务：

```bash
proxy_watch/.venv/bin/python tools/mihomo/configure.py --node "$NODE"
```

候选保存在 `~/mihomo/private/config.candidate.yaml`，文件 `0600`、目录 `0700`。它包含节点凭据，不能上传。已有同名候选时脚本停止，由你保留/移走后重新准备。生成配置只监听本机 17890，关闭 LAN/控制接口/TUN/DNS，单节点 MATCH 路由；保留原始受支持 TLS/ECH/AnyTLS 字段。

**仅首次安装、没有正式 config 时**，用下条命令将已验证候选写为正式配置。它会再次原生校验，原子创建，不会覆盖已有 `config.yaml`：

```bash
proxy_watch/.venv/bin/python tools/mihomo/activate_initial.py
mkdir -p "$HOME/.config/systemd/user"
cp -n tools/mihomo/mihomo-proxy.service.example "$HOME/.config/systemd/user/mihomo-proxy.service"
systemctl --user daemon-reload
systemctl --user cat mihomo-proxy.service
```

`cp -n` 保留已有 unit。核对 `systemctl --user cat` 显示的路径与本指南一致；已有 unit 使用不同工作目录或配置时，沿用原服务并相应调整 watcher settings，不能当作新模板已经生效。确认后显式启动 Mihomo：

```bash
systemctl --user enable --now mihomo-proxy.service
systemctl --user is-active mihomo-proxy.service
proxy_watch/.venv/bin/python -B proxy_watch/proxy_watch.py check --config proxy_watch/settings.yaml
```

用户服务需在退出 SSH 后继续运行时，检查 `loginctl show-user "$USER" -p Linger`；若为 `no`，由有权限的管理员执行 `sudo loginctl enable-linger "$USER"`。无需改成 root 服务。

## 5. 单独核对 OKX API 白名单

**上述测试只证明公开 OKX 与 ipify 可达；不能证明 OKX 私有 API 白名单、读取权限或交易权限可用。** ipify 返回 IPv4 时，OKX 仍可能看到代理远端的 IPv6 或另一出口。配置中的 `ipv6: false` 也不能保证远端代理对 OKX 使用 IPv4。

对每条准备启用的候选线路，人工确认 OKX 实际看到的出口，并为每一个共用 17890 的 API key 登记正确 IPv4/IPv6 白名单，再执行相应策略的只读账户/权限检查。只读账户检查不得创建订单；具体 G3 命令见 [服务器部署指南](DEPLOYMENT.md)。如果遇到 HTTP 401、OKX `50110`，按返回的实际来源 IP（可能是 IPv6）修正白名单，不能仅照抄 ipify IPv4。私有权限失败时不能宣称“已验证可自动交易”。

然后才考虑将已独立核实的 ipify 出口精确写入 `allowed_ips`，并按 [代理看守说明](../proxy_watch/README.md) 显式启用。该列表只控制公共 ipify 门禁，不会修改 OKX 白名单。若无法保证所有候选的私有访问保持可用，保留空列表或停用自动切换；看守不接收交易 key，也不会替你做私有检查。

## 6. 已有生产环境：维护与排障

不要用本节前面的初始化步骤覆盖在用代理。先确认所有共用代理的程序已按各自维护流程暂停交易，并停止 `proxy-watch.service`；更新订阅或正式 config、手动换线期间持续保持看守停止。候选 `--list`/`probe_nodes.py` 可复用，正式变更应保留旧 config、原生校验、计划重启并验证17890，再执行每个程序的只读私有账户检查。

本版本已修复根 URL 请求路径和 VLESS/ECH、AnyTLS schema；不应再运行旧版更新脚本，把已修复文件覆盖回去。历史上曾出现候选节点公开探测健康、三次显式 `once --apply` 累积故障后成功切到生产、随后 `check` 健康，但私有账户仍返回 `50110` 的情况。它只能证明公共换线链路有效，实际 OKX IPv6 仍需人工白名单登记；该节点编号只是旧订阅的记录，不是新安装默认值。

常用诊断（输出可能含节点/出口信息，分享前先脱敏）：

```bash
systemctl --user status mihomo-proxy.service --no-pager
journalctl --user -u mihomo-proxy.service -n 40 --no-pager
tail -n 40 "$HOME/mihomo/private/proxy.log"
proxy_watch/.venv/bin/python -B proxy_watch/proxy_watch.py check --config proxy_watch/settings.yaml
```

`validate` 是结构校验，不是所有节点在线测试；有任意被拒节点即返回 2，即使其余节点可用。因此请单独执行并阅读结果，**不要**写成 `validate && probe_nodes.py` 导致混合订阅不再探测。没有正式 config 的新机器应先用 `configure.py --list`，首次激活后再 `validate`。

工具离线测试：

```bash
proxy_watch/.venv/bin/python -B -m unittest discover -s tools/mihomo/tests -v
PYTHONPATH=proxy_watch proxy_watch/.venv/bin/python -B -m unittest discover -s proxy_watch/tests -v
```
