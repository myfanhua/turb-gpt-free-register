# Turb GPT Register 部署指南

## 服务器信息
- 服务器: 64.83.20.57
- 部署路径: /opt/turb-gpt-register
- 内部端口: 5100
- 对外域名: register.feixueapi.xyz (需配置 nginx)

## 部署步骤

### 1. SSH 登录服务器
```bash
ssh root@64.83.20.57
```

### 2. 克隆项目
```bash
cd /opt
git clone https://github.com/myfanhua/turb-gpt-free-register.git turb-gpt-register
cd turb-gpt-register
```

### 3. 创建虚拟环境并安装依赖
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 4. 安装 Playwright（可选，如果使用 cloak 驱动）
```bash
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps
```

### 5. 创建配置文件 .env
```bash
cat > .env <<'EOF'
# WebUI 授权
WEBUI_AUTH_CODE="admin123456"
WEBUI_SESSION_SECRET="$(openssl rand -hex 32)"

# 功能开关
ENABLE_CODEX_AUTO="False"

# 注册驱动（protocol/roxy/cloak/browser_use/skyvern）
REGISTRATION_DRIVER="cloak"

# CloakBrowser 配置
CLOAK_HEADLESS="True"
CLOAK_HUMANIZE="True"
CLOAK_GEOIP="True"
CLOAK_USE_PROXY="False"

# 邮箱来源
EMAIL_SOURCE="outlook"

# 并发配置
MAX_WORKERS="1"
EOF
```

### 6. 创建 systemd 服务
```bash
cat > /etc/systemd/system/turb-gpt-register.service <<'EOF'
[Unit]
Description=Turb GPT Register WebUI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/turb-gpt-register
ExecStart=/opt/turb-gpt-register/.venv/bin/python web.py --host 127.0.0.1 --port 5100
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

### 7. 启动服务
```bash
systemctl daemon-reload
systemctl enable turb-gpt-register
systemctl start turb-gpt-register
systemctl status turb-gpt-register
```

### 8. 配置 nginx 反向代理
```bash
cat > /etc/nginx/sites-available/turb-gpt-register <<'EOF'
server {
    listen 80;
    server_name register.feixueapi.xyz;
    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name register.feixueapi.xyz;

    # SSL 证书（使用现有证书或用 certbot 生成）
    ssl_certificate /etc/letsencrypt/live/register.feixueapi.xyz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/register.feixueapi.xyz/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5100;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket 支持
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # 超时
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    location /static/ {
        alias /opt/turb-gpt-register/static/;
        expires 30d;
    }

    access_log /var/log/nginx/turb-gpt-register-access.log;
    error_log /var/log/nginx/turb-gpt-register-error.log;
}
EOF

# 启用站点
ln -sf /etc/nginx/sites-available/turb-gpt-register /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

### 9. 停止旧的注册机服务（可选）
如果要完全替换旧服务：
```bash
systemctl stop zcj-register
systemctl disable zcj-register
```

## 验证部署

1. 检查服务状态：
```bash
systemctl status turb-gpt-register
journalctl -u turb-gpt-register -f
```

2. 测试内部访问：
```bash
curl http://127.0.0.1:5100
```

3. 访问 WebUI：
```
https://register.feixueapi.xyz
```

默认登录密码：admin123456

## 常用命令

```bash
# 查看日志
journalctl -u turb-gpt-register -f

# 重启服务
systemctl restart turb-gpt-register

# 停止服务
systemctl stop turb-gpt-register

# 更新代码
cd /opt/turb-gpt-register
git pull
systemctl restart turb-gpt-register

# 修改配置
vim /opt/turb-gpt-register/.env
systemctl restart turb-gpt-register
```

## 项目特点

- 支持多种注册驱动（protocol/roxy/cloak/browser_use/skyvern）
- 支持多种邮箱来源（Outlook/Cloudflare/IMAP/API）
- 支持 Codex OAuth 授权
- WebUI 实时查看任务日志
- SQLite 数据库存储账号和任务
- 支持批量注册和补跑

## 下一步

部署完成后，你可以：
1. 登录 WebUI 配置邮箱来源
2. 配置代理（如果需要）
3. 选择注册驱动
4. 开始批量注册任务
5. 根据需要调整界面样式
