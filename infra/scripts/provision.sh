#!/usr/bin/env bash
# 一键部署 caiji-mvp 到阿里云 ECS (cn-hangzhou)
# 复刻 zhaoshang infra/scripts/provision-https.sh 的"无 SSH"模式
# 通过阿里云 ECS RunCommand 远程执行
#
# 用法:
#   bash infra/scripts/provision.sh           # 全流程
#   bash infra/scripts/provision.sh code      # 仅同步代码 (git pull)
#   bash infra/scripts/provision.sh deps      # 仅装依赖
#   bash infra/scripts/provision.sh service   # 仅写 systemd + 启动服务
#   bash infra/scripts/provision.sh nginx     # 仅配 nginx
#   bash infra/scripts/provision.sh cert      # 仅申请 SSL 证书
#   bash infra/scripts/provision.sh verify    # 仅验证

set -euo pipefail

# ============ 配置 ============
DOMAIN="caiji.tianku.com"
EMAIL="lianqiu727@gmail.com"
INSTANCE_ID="i-bp18zrqcsw2yuxmy6yd2"
REGION="cn-hangzhou"
REPO_URL="https://github.com/l6444585-afk/caiji-mvp.git"
DEPLOY_DIR="/opt/fengyun/caiji-mvp"
NGINX_CONF_REMOTE="/etc/nginx/sites-available/caiji"
NGINX_CONF_LOCAL="$(cd "$(dirname "$0")/.." && pwd)/nginx/caiji.conf"
SYSTEMD_UNIT_LOCAL="$(cd "$(dirname "$0")/.." && pwd)/systemd/caiji-mvp.service"

# ============ Step 1: 拉取代码 + 装依赖 ============
deploy_code_and_deps() {
  echo "==> Step 1: git clone/pull + 装 Python venv + 依赖"
  local script
  script=$(cat <<SHELL
set -e
mkdir -p $DEPLOY_DIR
if [ -d $DEPLOY_DIR/.git ]; then
  cd $DEPLOY_DIR && git fetch --all && git reset --hard origin/main
else
  git clone $REPO_URL $DEPLOY_DIR
fi
cd $DEPLOY_DIR
apt-get install -y -qq python3-venv python3-pip
[ ! -d .venv ] && python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "✅ 代码同步 + 依赖装好"
.venv/bin/python -c "import fastapi; import uvicorn; print('FastAPI', fastapi.__version__, 'uvicorn', uvicorn.__version__)"
SHELL
  )
  invoke_remote "$script"
}

# ============ Step 2: 写 systemd unit + 启动 ============
setup_systemd() {
  echo "==> Step 2: 配 systemd unit + 启动 caiji-mvp 服务"
  local unit_b64
  unit_b64=$(base64 < "$SYSTEMD_UNIT_LOCAL" | tr -d '\n')
  local script
  script=$(cat <<SHELL
set -e
echo "$unit_b64" | base64 -d > /etc/systemd/system/caiji-mvp.service
systemctl daemon-reload
systemctl enable caiji-mvp
systemctl restart caiji-mvp
sleep 2
systemctl is-active caiji-mvp
echo "--- 服务状态 ---"
systemctl status caiji-mvp --no-pager | head -15
echo "--- 端口监听 ---"
ss -tlnp | grep 8090 || echo "端口未监听!"
echo "--- 本机健康检查 ---"
curl -sS http://127.0.0.1:8090/ | head -c 200
SHELL
  )
  invoke_remote "$script"
}

# ============ Step 3: 写 nginx server block ============
setup_nginx() {
  echo "==> Step 3: 配 nginx caiji server block (HTTP only 第一次)"
  local conf_b64
  conf_b64=$(base64 < "$NGINX_CONF_LOCAL" | tr -d '\n')
  # 首次部署只配 HTTP 80, certbot 之后会自动加 443
  local script
  script=$(cat <<SHELL
set -e
# 临时配置只监听 80 (certbot 会改造成 443)
cat > $NGINX_CONF_REMOTE <<'NGINX_EOF'
server {
    listen 80;
    server_name $DOMAIN;
    location /onebot/event {
        proxy_pass http://127.0.0.1:8090/onebot/event;
        proxy_set_header Host \\\$host;
        proxy_set_header X-Real-IP \\\$remote_addr;
        client_max_body_size 5M;
    }
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host \\\$host;
        proxy_set_header X-Real-IP \\\$remote_addr;
    }
}
NGINX_EOF
ln -sf $NGINX_CONF_REMOTE /etc/nginx/sites-enabled/caiji
nginx -t
systemctl reload nginx
echo "✅ nginx caiji server block (HTTP) 已加"
curl -sS -H "Host: $DOMAIN" http://127.0.0.1/ | head -c 200
SHELL
  )
  invoke_remote "$script"
}

# ============ Step 4: certbot 申请 HTTPS ============
provision_cert() {
  echo "==> Step 4: certbot --nginx 申请 Let's Encrypt 证书"
  local script
  script=$(cat <<SHELL
set -e
export DEBIAN_FRONTEND=noninteractive
which certbot >/dev/null 2>&1 || apt-get install -y -qq certbot python3-certbot-nginx
certbot --nginx -d $DOMAIN \\
  --non-interactive --agree-tos --redirect \\
  --email $EMAIL
nginx -t && systemctl reload nginx
echo "✅ HTTPS 配置完成"
SHELL
  )
  invoke_remote "$script"
}

# ============ Step 5: 部署完整加固 nginx (替换 certbot 自动生成的) ============
deploy_hardened_nginx() {
  echo "==> Step 5: 部署 Mozilla intermediate / A+ 加固 nginx 配置"
  local conf_b64
  conf_b64=$(base64 < "$NGINX_CONF_LOCAL" | tr -d '\n')
  local script
  script=$(cat <<SHELL
set -e
TS=\$(date +%Y%m%d-%H%M%S)
cp $NGINX_CONF_REMOTE $NGINX_CONF_REMOTE.bak.\$TS
echo "$conf_b64" | base64 -d > /tmp/caiji.new
mv /tmp/caiji.new $NGINX_CONF_REMOTE
if ! nginx -t 2>&1; then
  cp $NGINX_CONF_REMOTE.bak.\$TS $NGINX_CONF_REMOTE
  echo "!!! ROLLED BACK"
  exit 1
fi
systemctl reload nginx
echo "✅ 加固配置部署完成"
SHELL
  )
  invoke_remote "$script"
}

# ============ Step 6: 验证 ============
verify() {
  echo "==> Step 6: 验证 HTTPS / 服务 / 端到端"
  local script
  script=$(cat <<SHELL
echo "=== 服务状态 ==="
systemctl is-active caiji-mvp
echo "=== 端口监听 ==="
ss -tlnp | grep 8090
echo "=== HTTPS 健康检查 ==="
curl -sS https://$DOMAIN/ | head -c 300
echo ""
echo "=== TLS handshake ==="
echo Q | openssl s_client -connect $DOMAIN:443 -servername $DOMAIN 2>&1 | grep -E 'subject=|issuer=|Protocol' | head -5
echo "=== 模拟 NapCat 上报 ==="
curl -sS -X POST https://$DOMAIN/onebot/event \\
  -H "Content-Type: application/json" \\
  -d '{"self_id":100,"user_id":1,"message_type":"group","sub_type":"normal","sender":{"nickname":"test"},"raw_message":"测试消息 9.9元 (testToken123) HU1234","post_type":"message","group_id":999,"group_name":"测试群"}' | head -c 200
echo ""
echo "=== 最近 5 条采集 ==="
curl -sS https://$DOMAIN/recent?limit=5 | head -c 500
echo ""
echo "=== 续期机制 ==="
systemctl is-enabled certbot.timer
SHELL
  )
  invoke_remote "$script"
}

# ============ Helper: 远程执行 ============
invoke_remote() {
  local script="$1"
  local b64 invoke_id status
  b64=$(printf '%s' "$script" | base64)
  invoke_id=$(aliyun ecs RunCommand --RegionId "$REGION" \
    --InstanceId.1 "$INSTANCE_ID" \
    --Type RunShellScript \
    --ContentEncoding Base64 \
    --CommandContent "$b64" \
    --Timeout 900 \
    --cli-query 'InvokeId' | tr -d '"')
  echo "InvokeId=$invoke_id"
  until [ "$(aliyun ecs DescribeInvocations --RegionId "$REGION" --InvokeId "$invoke_id" --cli-query 'Invocations.Invocation[0].InvokeStatus' | tr -d '"')" = "Finished" ]; do
    sleep 5
  done
  aliyun ecs DescribeInvocationResults --RegionId "$REGION" --InvokeId "$invoke_id" \
    --cli-query 'Invocation.InvocationResults.InvocationResult[0].Output' \
    | tr -d '"' | base64 -d
  status=$(aliyun ecs DescribeInvocationResults --RegionId "$REGION" --InvokeId "$invoke_id" \
    --cli-query 'Invocation.InvocationResults.InvocationResult[0].ExitCode')
  if [ "$status" != "0" ]; then
    echo "!!! ExitCode=$status, aborting"
    exit "$status"
  fi
}

# ============ Main ============
case "${1:-all}" in
  code)     deploy_code_and_deps ;;
  deps)     deploy_code_and_deps ;;
  service)  setup_systemd ;;
  nginx)    setup_nginx ;;
  cert)     provision_cert ;;
  hardened) deploy_hardened_nginx ;;
  verify)   verify ;;
  all)
    deploy_code_and_deps
    setup_systemd
    setup_nginx
    provision_cert
    deploy_hardened_nginx
    verify
    ;;
  *)
    echo "Usage: $0 [code|service|nginx|cert|hardened|verify|all]"
    exit 1
    ;;
esac

echo "==> Done. https://$DOMAIN ready."
