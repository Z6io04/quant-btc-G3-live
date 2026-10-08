#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
  echo '请使用dvens普通用户，不要sudo运行安装脚本。' >&2
  exit 1
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ "$project_dir" != "$HOME/btc-G3-live" ]; then
  echo '请把完整目录放到~/btc-G3-live，再运行安装脚本。' >&2
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "需要Python 3.11或更新版本"'
if systemctl --user is-active --quiet btc-G3-live.service; then
  echo 'G3服务正在运行，保留现状；不要在持仓期间覆盖服务配置。' >&2
  exit 1
fi
mkdir -p "$HOME/.config/systemd/user"
unit_file="$HOME/.config/systemd/user/btc-G3-live.service"
if [ -e "$unit_file" ] || [ -L "$unit_file" ]; then
  if [ -L "$unit_file" ] || ! cmp -s "$unit_file" "$project_dir/deploy/btc-G3-live.service"; then
    echo '已有不同的服务配置，保留原文件，请先核对。' >&2
    exit 1
  fi
else
  install -m 600 "$project_dir/deploy/btc-G3-live.service" "$unit_file"
fi
systemctl --user daemon-reload
echo '用户服务已安装；没有启动、启用或下单。完成arm-live后，由你执行systemctl --user enable --now btc-G3-live.service。'
