# Mihomo 初始化工具

按 [代理安装指南](../../docs/PROXY-SETUP.md) 依次操作；所有 Python 命令从仓库根目录运行。已有生产配置不会被本工具覆盖。

| 工具 | 用途 |
| --- | --- |
| `install.py` | Linux amd64：获取官方稳定版与可信 SHA256，验证后首次安装；不启动服务 |
| `download_subscription.py` | 通常在 Mac：隐藏输入 HTTPS 订阅 URL，保存私有文件 |
| `configure.py --list` | 列出本次订阅的原始 1-based 编号与支持情况；不显示凭据 |
| `configure.py --node N` | 使用看守同一 schema 生成 `config.candidate.yaml`，原生 `mihomo -t` 校验 |
| `activate_initial.py` | 再次验证候选，仅在生产配置不存在时创建 `config.yaml` |
| `mihomo-proxy.service.example` | `%h/mihomo` 用户服务模板，端口由配置固定为本机 17890 |

`configure.py` 与 `activate_initial.py` 用 `proxy_watch/.venv/bin/python` 执行，需要 PyYAML。候选联网测试使用 `proxy_watch/probe_nodes.py`，其编号也从 1 开始；自动看守独立启用，见 [看守说明](../../proxy_watch/README.md)。初始化工具不启用看守、不写 IP 白名单、不接收交易 keys、不执行私有账户请求。

离线测试：

```bash
proxy_watch/.venv/bin/python -B -m unittest discover -s tools/mihomo/tests -v
```

测试使用合成节点、合成 release/ELF、假原生验证器及模拟下载；不证明真实下载、systemd 或服务器网络已经验收。正式启动前仍需真实 Mihomo 验证、公开探测与各策略的只读账户检查。
