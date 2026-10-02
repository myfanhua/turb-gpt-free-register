#!/bin/bash
set -e

echo "开始部署 Turb GPT Register..."

# 1. 创建部署目录
sudo mkdir -p /opt/turb-gpt-register
sudo chown root:root /opt/turb-gpt-register

# 2. 上传项目文件（需要在本地执行）
# rsync -avz --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' /home/hatch/dsh-harness/home/ds/turb-gpt-register/ root@64.83.20.57:/opt/turb-gpt-register/

cd /opt/turb-gpt-register

# 3. 创建虚拟环境
python3 -m venv .venv

# 4. 安装依赖
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 5. 安装 Playwright 浏览器
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps

# 6. 创建 .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo "请编辑 .env 配置文件"
fi

# 7. 创建 systemd 服务
cat > /etc/systemd/system/turb-gpt-register.service <<'EOF'
[Unit]
Description=Turb GPT Register WebUI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/turb-gpt-register
ExecStart=/opt/turb-gpt-register/.venv/bin/python web.py --host 127.0.0.1 --port 5000
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 8. 重载 systemd
systemctl daemon-reload
systemctl enable turb-gpt-register

echo "部署完成！"
echo "下一步："
echo "1. 编辑 /opt/turb-gpt-register/.env 配置"
echo "2. 配置 nginx 反向代理"
echo "3. systemctl start turb-gpt-register"
