# 从下载到运行：G3 部署全流程

先判断这是**已有运行安装**还是**全新安装**。本包的核心代码与原 G3 相同，重新打包并不要求正在运行的策略重新初始化。服务名为 `btc-G3-live.service`，部署目录必须是普通用户的 `$HOME/btc-G3-live`。

## 已有安装：只检查，不重装

本次已知运行环境已经启用 G3，预算 30U、累计亏损停止金额 10U，服务为 active。保留它的配置、API、账本、账户登记和服务文件。不要把交付 zip 解压覆盖 `$HOME/btc-G3-live`；不要复制默认 `config.json` 到服务器；不要重新 `arm-live`；不要运行 F3 迁移工具。即使代码相同，也不需要借重装来“刷新”账户。

在**服务器 SSH 终端**中执行：

```bash
python3 "$HOME/btc-G3-live/launch.py" status
systemctl --user is-active btc-G3-live.service
systemctl --user is-enabled btc-G3-live.service
journalctl --user -u btc-G3-live.service -n 80 --no-pager
```

必要时再执行独立只读检查：

```bash
python3 "$HOME/btc-G3-live/launch.py" check
python3 "$HOME/btc-G3-live/launch.py" account-check
```

`status` 读取本地记录并探测公开网络；`account-check` 读取账户和资金计划。它们都不发送订单。活跃服务可能同时继续正常交易，所以检查结果是各自时点的快照，不能把一次空仓输出当成永久状态。详见 [验收](VERIFY-AFTER-INSTALL.md) 和 [运维](OPERATIONS.md)。

源码参考可以留在 Mac 或另外的资料目录；不要从参考目录启用第二份交易进程。已有 Mihomo/看守需要维护时，遵循 [代理配置的已有安装路线](PROXY-SETUP.md)，不要套用首次代理激活流程。

## 全新部署

以下只适用于新目录、尚未启用的 G3 和干净独立子账户。每一步报错时先解决该步，再继续；不要靠删除 `private/`、`var/` 或账户登记来跳过报错。

### 1. 分清 Mac 终端与服务器终端

- **Mac 本地终端**：用于上传文件和执行 `ssh`。这里 `$HOME` 是 Mac 的用户主目录。
- **服务器 SSH 终端**：登录成功后用于安装依赖、配置和运行服务。这里 `$HOME` 是服务器普通用户主目录，通常为 `/home/你的用户名`。
- 文中的 `YOUR_USER`、`YOUR_SERVER`、`YOUR_GITHUB_NAME`、`YOUR_REPOSITORY` 都要替换；不要把占位文字照抄成真实地址。命令前不带 `$` 或提示符。
- G3 使用 Linux 普通用户运行。日常 Python 和 `systemctl --user` 不加 `sudo`；系统包和开启 linger 才可能需要管理员权限。

### 2. 上传或从 GitHub 获取代码（二选一）

**方式 A：上传交付 zip。** 将 `btc-G3-live-github-20261008.zip` 放在 Mac 桌面，在 **Mac 本地终端**运行：

```bash
scp "$HOME/Desktop/btc-G3-live-github-20261008.zip" YOUR_USER@YOUR_SERVER:~/
ssh YOUR_USER@YOUR_SERVER
```

登录后先确认身份、位置和目录是否已存在：

```bash
whoami
printf '%s\n' "$HOME"
ls -ld "$HOME/btc-G3-live"
```

`ls` 显示“没有那个文件或目录”才符合新装条件。若目录存在，停在这里，使用上面的已有安装路线，不覆盖。确认目录不存在后在**服务器**解压：

```bash
python3 -m zipfile -e "$HOME/btc-G3-live-github-20261008.zip" "$HOME"
cd "$HOME/btc-G3-live"
pwd
ls launch.py config.json deploy/install-service.sh
```

该 zip 内只有一层 `btc-G3-live/` 根目录，解压位置是 `$HOME`，不是 `$HOME/btc-G3-live`；否则会多套一层。若系统尚无 Python，先完成下一步安装 Python 再返回解压。

**方式 B：从已上传的 GitHub 仓库克隆。** 先从 Mac SSH 登录，在服务器确认 `$HOME/btc-G3-live` 不存在；安装下一步的 `git` 后执行：

```bash
cd "$HOME"
git clone https://github.com/YOUR_GITHUB_NAME/YOUR_REPOSITORY.git btc-G3-live
cd "$HOME/btc-G3-live"
ls launch.py config.json deploy/install-service.sh
```

必须显式写目标目录 `btc-G3-live`，不要沿用自动生成的仓库名。私有仓库需先配置你自己的 GitHub 访问方式，不把访问令牌粘进命令、源码或聊天。下载 GitHub 的 “Code → Download ZIP” 会产生仓库名加分支名的文件夹；将**包含 `launch.py` 的完整文件夹**放到 `$HOME/btc-G3-live`，不要把外层包装目录当成部署目录。方式 A 与 B 不需要都执行。

### 3. 准备 Debian 依赖

在**服务器 SSH 终端**执行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl ca-certificates tar
python3 --version
python3 -c 'import sys; assert sys.version_info >= (3,11), "G3需要Python 3.11或更新版本"'
```

G3 核心只依赖 Python 标准库，不需要给系统 Python 安装一堆交易库。可选代理看守需要 PyYAML，并使用它自己的虚拟环境。Debian 上没有 `python3-venv` 常会导致 `ensurepip is not available`；安装此系统包后重新创建环境，不能跳过依赖安装继续运行。

查看时间同步，避免错误的系统时间影响签名和网络检查：

```bash
timedatectl status
```

### 4. 安装并检查 Mihomo 和代理看守

先按 [代理完整配置](PROXY-SETUP.md) 的第 1—4 节完成：安装适用的 Mihomo 二进制、创建看守虚拟环境、保存自己的本地订阅、选择/测试节点、生成首次正式配置、安装并启动 `mihomo-proxy.service`。这一阶段只要求生产代理公开访问可用；看守的 `allowed_ips` 保持空列表，暂不启用自动切换。该文档区分首次安装和已有生产代理，避免覆盖已运行的 17890。

代理文档第 5 节的私有 API 检查，需要先完成本页后面的资金和密钥配置。等本页第 8 步账户检查成功，再返回核对每条候选的实际 OKX 白名单与允许出口，决定是否启用看守。

G3 默认代理配置已经是：

```json
"proxy": "http://127.0.0.1:17890"
```

这是 `config.json` 的一个字段，不是一份完整配置；不要用这三行覆盖整个文件。通常保持默认即可。所有 G3 行情和账户请求都会使用该本机 HTTP 入口，不读取 `.env`、`HTTP_PROXY`、Mac 系统代理或浏览器代理。首次启用前如确有不同本机端口，才在完整 `config.json` 中修改；启用后配置被身份指纹锁定，不能随意改动。

```bash
systemctl --user is-active mihomo-proxy.service
python3 "$HOME/btc-G3-live/launch.py" check
```

服务 `active` 仅表示进程活着；`check` 才会通过该入口访问公开接口。此时尚未填 API/预算也可以做公开检查，不下单。看守当前线路 `check` 失败不会触发切换；候选探测和显式自动切换的区别见代理文档。

### 5. 在欧易手动准备独立子账户与 API

这个子账户只给 G3 使用，不与 F3、v53、手动交易或另一份 G3 进程混用。在欧易界面核实以下设置和状态，程序不会替你修改：

| 项目 | 要求 |
|---|---|
| 账户模式 | 合约模式（原单币种保证金；接口 `acctLv=2`） |
| 持仓模式 | 买卖/净持仓模式（`net_mode`） |
| 合约 | **BTC-USD-SWAP** 币本位永续，**全仓 2 倍**，不是 BTC-USDT-SWAP |
| 初次启用 | 无持仓，无普通/条件/算法挂单，BTC 余额为 0，无借币/负债 |
| 资金 | 预算范围内的 USDT 已在该子账户的**交易账户**，可用余额足够 |
| API | 实盘 API，读取和交易权限，不启用提币；保存 Key、Secret、Passphrase |
| API IP 白名单 | 在欧易手动填入该 API 请求真正使用的公网出口，覆盖你允许切换的线路 |

节点的服务器地址不是你的公网出口。`api.ipify.org` 返回的 IP 也不一定等于 OKX 接收到的 IP；某些节点会分别走 IPv4 和 IPv6。看守的 `allowed_ips` 是本地切换门禁，不能代替欧易白名单，也不会同步白名单。共用 17890 的其他策略/API 同样需要适配允许出口。

若只读检查明确提示旧 F3 绑定，先看 [F3 受控迁移](F3-MIGRATION.md)。迁移辅助工具只用于尚未启用 G3 的严格受控场景，不把“删除旧文件”作为绕过独立账户保护的方法。

### 6. 填写预算和累计亏损停止金额

在服务器的交互终端执行：

```bash
cd "$HOME/btc-G3-live"
python3 launch.py setup-config
```

按提示填写首次投入 USDT 与累计亏损停止金额。两者须大于 0，亏损金额不大于本金。用该子账户实际计划投入的数字，不照搬历史回测的 25U 或现有安装的 30U/10U。当前价格、交易所最小合约数量、手续费和配平取整仍可能让预算不足，下一步会检查。

没有固定本金上限。追加资金必须等两腿空仓、账本已核对、服务停止后，由你运行 `sync-capital`；不会自动读取全部余额作为本金。累计亏损金额限额不会随充值自动变大，单组损失 3%和账户回撤 5%等原规则仍有效。所有退出条件都不保证实际最大损失。

### 7. 交互保存 API

```bash
python3 launch.py setup-credentials
```

按提示粘贴 Key、Secret、Passphrase，输入时屏幕不回显字符是正常现象，按回车提交。工具拒绝命令行参数或管道输入，避免把密钥留在命令历史里。文件保存为 `private/credentials.json`，权限为仅当前用户可读写。不使用 `credentials.env` 或 `.env`。

工具不会覆盖已存在的凭据或已启用账本。填错时保留现场按 [运维说明](OPERATIONS.md) 处理，不在已启用安装中直接替换 API。不要用 `cat` 显示密钥来截图求助。

### 8. 只读验收：先联网，再账户

```bash
python3 launch.py check
python3 launch.py account-check
```

| 结果 | 应如何理解 |
|---|---|
| `check` 的 `passed: true` | 公开行情/历史数据检查有可用结果，订单数为 0 |
| `funding_signal.allowed: false` | 资金费等入场条件不满足，可能是正常等待；不表示必须换节点 |
| `account-check` 的 `private_read_verified: true` | 本次可读取账户；它没有发送测试订单，也不能证明真实下单权限一定可用 |
| `entry_budget_plan.allowed: true` | 当前预算和当前市场条件下，可生成两腿资金计划；不是入场指令 |
| `position_present` 或 `pending_orders` 非空 | 新装前先在交易所核对来源；不得直接启用 |

如果检查报错，不要直接跳到 `arm-live`。预检涉及时间、报价新鲜度、价差、产品规格、费率、账户模式、白名单和旧绑定；失败不等于一定是 API key 错。HTTP 401 可先执行包内只读诊断：

```bash
python3 -B tools/diagnose_account.py
```

诊断只请求公开时间和只读账户配置，尽量给出脱敏错误码；IP 白名单错误如返回明确的 OKX 所见出口会提示该出口，仍由你在欧易手动处理。更多排查见 [运维](OPERATIONS.md)。

账户只读检查通过后，如计划启用自动切换，返回 [代理完整配置第 5 节](PROXY-SETUP.md#5-单独核对-okx-api-白名单)，先独立核实候选线路的实际 OKX 白名单与共用 API，再填写允许出口并按看守文档显式启用。只验证当前线路，不代表其他候选也能访问私有 API；尚未核实就保持自动切换关闭。

### 9. 明确启用 G3 身份（本命令不下单）

确认这是全新安装、正确独立子账户、实际空仓无挂单，前面的检查通过后执行：

```bash
python3 launch.py arm-live
```

阅读显示的预算、亏损金额与真钱提示，仅在确认要启用时输入：

```text
ARM G3 REAL MONEY
```

成功结果应为 `armed: true`、`orders_sent: 0`。该步登记账户、绑定目录、固定代码/API/配置指纹并建立账本。只能首次成功启用一次，失败时保留现场查原因；成功后日常使用 `run`/`status`，不再初始化。

### 10. 安装用户服务与退出 SSH 后继续运行

```bash
bash "$HOME/btc-G3-live/deploy/install-service.sh"
loginctl show-user "$(id -un)" -p Linger
```

安装脚本只安装 `btc-G3-live.service` 并重载用户服务，不启动、不启用、不下单。要求目录恰好是 `$HOME/btc-G3-live`，普通用户运行；已有不同服务文件或正在运行的 G3 会阻止覆盖。

如果 `Linger=no`，由有管理员权限的人执行一次：

```bash
sudo loginctl enable-linger "$(id -un)"
loginctl show-user "$(id -un)" -p Linger
```

`Linger=yes` 配合服务 `enabled`，让用户服务在退出 SSH 后继续运行，并可在系统重启后启动。只有 `active` 而未 `enabled` 不能证明重启自启已配置。所有日常服务命令仍使用普通用户的 `systemctl --user`，不要改为 root 系统服务。

### 11. 启动具有真实下单权限的循环

**下面这一步会启动真钱交易循环。** 不要同时再开一个前台 `python3 launch.py run`。

```bash
systemctl --user enable --now btc-G3-live.service
systemctl --user is-active btc-G3-live.service
systemctl --user is-enabled btc-G3-live.service
python3 "$HOME/btc-G3-live/launch.py" status
```

未成功 `arm-live` 时服务不能自行启用交易；已成功启用时，服务会在策略条件满足后提交真实订单。空仓不表示启动失败，G3 仍须满足资金费、成本覆盖、冷却、资金取整、账本核对与风控条件。

继续执行 [部署后验收](VERIFY-AFTER-INSTALL.md)，日常使用 [运维说明](OPERATIONS.md)。不要为了让它马上开仓反复重启、改参数、删账本或重复 `arm-live`。
