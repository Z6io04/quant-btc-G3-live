# 把交付包保存到 GitHub

使用干净的 `btc-G3-live-github-20261008.zip` 作为上传来源。不要从已经运行的服务器目录重新压缩上传。GitHub 只保存代码与文档，不会替你运行服务、保存交易状态或替你配置交易 API。

## 先确认上传边界

仓库根目录应直接看到 `README.md`、`launch.py`、`config.json`、`g3core/`、`docs/`、`proxy_watch/` 等内容，不再套一层 `btc-G3-live/`。交付 zip 为了便于服务器解压含有这层目录；上传仓库时进入这层后选择**里面的内容**。

可以上传本包的源码、示例、测试、部署工具和已脱敏报告。不要上传：

| 私有内容 | 原因 |
|---|---|
| `private/`、`credentials*.json`、`.env` | API 与账户秘密 |
| `var/`、`*.sqlite3*`、运行日志 | 真钱交易账本、订单、账户状态 |
| `proxy_watch/settings.yaml`、正式 Mihomo `config.yaml` | 实际路径、出口与代理凭据 |
| 本地订阅、任何真实节点 URL/UUID/密码 | 订阅和代理访问秘密 |
| `~/mihomo/private/`、看守备份和事务文件 | 可能含完整原始代理配置 |
| `.venv/`、`__pycache__/`、本机工具缓存 | 与可复现源码无关，体积大且含本机信息 |
| `$HOME/.local/state/` 中的账户绑定/锁/状态 | 运行身份记录，只留可信私有备份 |

`.gitignore` 帮助 Git 命令忽略上述常见文件，但不能保护之前已经跟踪的文件；**GitHub 网页拖拽不会替你应用本地 `.gitignore` 过滤文件**。无论哪种方式，都要先检查实际上传列表。保留本包的空预算 `config.json` 和 `settings.example.yaml`，不要替换为服务器已配置的版本。

## 方法一：GitHub 网页上传（适合初次使用）

1. 在 Mac 解压交付 zip，打开其中的 `btc-G3-live` 文件夹。使用 Finder 的 `Command + Shift + .` 显示隐藏文件，确认 `.gitignore` 也在其中。
2. 在 GitHub 创建一个新仓库，名称由你决定。先按自己的需求选择可见范围；初次保存可先用 Private。不要勾选自动创建 README、`.gitignore` 或 License，避免与交付文件冲突。包里没有替你作出的开源许可决定。
3. 在空仓库选择 “uploading an existing file” 或 “Add file → Upload files”。拖入 `btc-G3-live` **文件夹内部的源码和子目录**，包括 `.gitignore`，不是拖入外层目录，也不是只上传 zip。
4. 查看上传清单：仓库根目录应出现 `launch.py`；不得出现真实凭据、订阅、`private/`、`var/`、已配置 `settings.yaml` 或运行账本。
5. 填写提交说明，例如 `Add G3 source, proxy tools and deployment docs`，确认提交。
6. 提交后打开根 README，点击部署/代理文档，确认目录和相对链接正常；打开 `config.json`，确认 `capital_usdt` 与 `max_loss_usdt` 都是 `null`，不含 API。

网页上传如果因文件数量或大小失败，改用下方 Git 方法。不要为了凑一次上传而把私有运行目录打包进去。历史行情数据包另行保存，不需要上传到源码仓库；回测报告和数据定位见 [回测说明](../backtesting/README.md)。

## 方法二：在 Mac 用 Git 提交和推送

在 GitHub 创建**空仓库**后，在 **Mac 本地终端**进入干净的交付目录。下面假设你解压到了桌面，实际不同就替换路径；不是进入服务器的运行目录：

```bash
cd "$HOME/Desktop/btc-G3-live"
ls launch.py README.md .gitignore
git init
git branch -M main
```

用明确的源码路径加入暂存区，不使用 `git add -f`：

```bash
git add .gitignore .github README.md CHANGELOG.md STRATEGY.md TEST-REPORT.md VERSION.json config.json launch.py g3core deploy docs tools tests proxy_watch backtesting reports
git status --short
git diff --cached --name-only
```

如果交付包还包含校验清单 `release-manifest.json`，确认它只是源码校验记录后可一并加入：

```bash
git add release-manifest.json
```

审阅文件清单，不应有上一节列出的私有内容。`git check-ignore` 可以检查规则，但输出并不代替上传审阅：

```bash
git check-ignore private/credentials.json var/live/state.sqlite3 proxy_watch/settings.yaml proxy_watch/.venv/example
```

若你误暂存了私有文件，先从暂存区移除并核对，不要继续推送。已经公开过的密钥须按实际情况撤销/轮换，删除工作目录文件不能删除 Git 历史里的秘密。

确认后提交；若 Git 首次提示姓名/邮箱未配置，按提示在此仓库配置自己的身份再重试：

```bash
git commit -m "Add G3 source, proxy tools and deployment docs"
git remote add origin https://github.com/YOUR_GITHUB_NAME/YOUR_REPOSITORY.git
git remote -v
```

把占位仓库地址换成自己刚创建的仓库，确认远端正确。下面一步才会上传到该远端：

```bash
git push -u origin main
```

认证使用你自己的 GitHub 登录/凭据管理或 SSH 配置，不把令牌直接写到 URL。若已存在 `origin`，先看 `git remote -v`，不要盲目添加或强推。不要使用 `--force` 覆盖已有仓库历史。

## 从 GitHub 安装到另一台服务器

上传完成后，从 [完整部署文档](DEPLOYMENT.md) 继续。克隆时显式指定目录名：

```bash
cd "$HOME"
git clone https://github.com/YOUR_GITHUB_NAME/YOUR_REPOSITORY.git btc-G3-live
```

只在 `$HOME/btc-G3-live` 不存在的服务器新装时使用。GitHub 仓库不含 API、订阅、运行状态；新机仍需完成代理安装、账户配置、只读检查和明确启用。不要把旧机的已启用账本当作全新安装复制启用，也不要让两台机器同时交易同一子账户。

## 后续版本和资料

运行后的 `config.json` 是本地已启用配置，不能因为 Git 提示有修改就还原。不要在活跃部署目录盲目 `git pull`、`git reset --hard` 或重新解压源码；代码/参数/API 身份变化会影响账本检查。先在独立资料目录比较版本，规划维护后再处理生产安装。

需要随仓库发布 zip 时，可另行使用 GitHub Release 附件；根目录源码仍应可直接浏览。`G3-backtest-data.zip` 是独立数据附件，源码包不包含其中行情大文件。是否公开仓库、发布附件以及采用何种许可证由仓库所有者决定，本次本地打包不代表已经发布到 GitHub。
