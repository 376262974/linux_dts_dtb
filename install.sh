#!/bin/sh
# dtbtool 在 Armbian / Debian 系系统上的安装脚本
# 用法:  sudo sh install.sh
#        sudo sh install.sh --with-service     # 额外安装开机自启的 Web 服务
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "请以 root 运行: sudo sh $0"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/dtbtool.py" ]; then
    echo "找不到 dtbtool.py, 请保证 install.sh 与 dtbtool.py 在同一目录。"
    exit 1
fi

echo "==> 更新软件源并安装 dtc ..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y device-tree-compiler

echo "==> 安装 dtbtool 到 /usr/local/bin/dtbtool ..."
install -m 755 "$SCRIPT_DIR/dtbtool.py" /usr/local/bin/dtbtool

if ! command -v dtc >/dev/null 2>&1; then
    echo "警告: dtc 安装后仍不可用, 请检查 apt 源。" >&2
else
    echo "dtc: $(dtc --version 2>&1 | head -n 1)"
fi

echo "==> 验证 ..."
dtbtool check || true

if [ "$1" = "--with-service" ]; then
    echo "==> 安装 Web 服务 (systemd) ..."
    cat > /etc/systemd/system/dtbtool-web.service <<'EOF'
[Unit]
Description=dtbtool Web UI for Device Tree editing
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/dtbtool web --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now dtbtool-web.service
    echo "Web 服务已启动: http://<设备IP>:8080"
    echo "注意: 默认监听 0.0.0.0, 仅限可信局域网使用。"
    echo "如需修改端口/地址: systemctl edit dtbtool-web"
fi

cat <<'EOF'

安装完成。常用命令:

  dtbtool check                          检查环境
  dtbtool list                           列出系统中的所有 DTB
  dtbtool info  /boot/dtb/xxx.dtb        查看 DTB 信息 (无需 dtc)
  dtbtool decompile board.dtb -o board.dts
  dtbtool compile  board.dts -o board.dtb --install
  dtbtool edit     /boot/dtb/xxx.dtb     反编译->编辑->写回(自动备份)
  dtbtool web --port 8080                启动浏览器编辑界面

提示: 修改 /boot 下的 DTB 前建议先在 armbianConfig 中关闭自动更新,
      避免内核升级覆盖你的改动。所有写回操作都会生成 .bak 备份。
EOF
