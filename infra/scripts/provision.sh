#!/usr/bin/env bash
# 一键部署 caiji-mvp 到阿里云 ECS (cn-hangzhou)
# 复刻 zhaoshang infra/scripts/provision-https.sh 的"无 SSH"模式
# 通过阿里云 ECS RunCommand 远程执行
#
# 用法:
#   bash infra/scripts/provision.sh           # 全流程
#   bash infra/scripts/provision.sh code      # 仅同步代码 (git pull)
#   bash infra/scripts/provision.sh deps      # 仅装依赖
#   bash infra/scripts/provision.sh secret    # 仅同步 .env (CAIJI_SECRET) 到 /etc/caiji-mvp.env
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
ENV_FILE_LOCAL="$(cd "$(dirname "$0")/../.." && pwd)/.env"
ENV_FILE_REMOTE="/etc/caiji-mvp.env"
# OSS 中转 bucket (cn-hangzhou, 跟 ECS 同 region 走内网下载 0 流量费)
OSS_BUCKET="caiji-deploy-hz"
OSS_ENDPOINT_PUBLIC="oss-cn-hangzhou.aliyuncs.com"
OSS_ENDPOINT_INTERNAL="oss-cn-hangzhou-internal.aliyuncs.com"

# ============ Step 1: 拉取代码 (本地 tar→OSS→ECS 内网拉) + 装依赖 ============
# 教训演进 (2026-05-14):
# v1 (失败): git fetch origin main → 阿里云→GitHub HTTP/2 framing 偶发失败, set -e 不退出
# v2 (失败): tar+base64 嵌进 RunCommand 脚本 → 撞 RunCommand 16KB 上限 (commit content 限制)
# v3 (当前): tar→OSS bucket (cn-hangzhou) → 内网 signed URL → RunCommand curl 下载解压
#   优点: ① 完全绕 GitHub ② 不撞 RunCommand 16KB 限制 (URL ~ 500 bytes)
#         ③ 内网下载零流量费 + 速度 100MB/s ④ OSS 文件保留 7 天可审计
deploy_code_and_deps() {
  echo "==> Step 1: 本地 tar → OSS (${OSS_BUCKET}) → ECS 内网拉 + 装依赖"
  local repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
  local ts
  ts=$(date +%s)
  local tar_path="/tmp/caiji-deploy-${ts}.tar.gz"
  local oss_key="caiji-deploy/${ts}.tar.gz"

  # 1. 本地打包 git 跟踪的所有文件
  (cd "$repo_root" && git ls-files -z | xargs -0 tar czf "$tar_path")
  echo "    tar size: $(wc -c < "$tar_path") bytes"

  # 2. 上传到 OSS (cn-hangzhou)
  if ! aliyun oss cp "$tar_path" "oss://${OSS_BUCKET}/${oss_key}" \
       --endpoint "$OSS_ENDPOINT_PUBLIC" --force >/dev/null 2>&1; then
    echo "!!! OSS upload failed; 检查 bucket '$OSS_BUCKET' 是否存在 + aliyun oss 凭证"
    rm -f "$tar_path"
    exit 1
  fi
  echo "    uploaded to oss://${OSS_BUCKET}/${oss_key}"

  # 3. 生成预签名 URL (1h, internal endpoint 让 ECS 走内网)
  local dl_url sign_out
  sign_out=$(aliyun oss sign "oss://${OSS_BUCKET}/${oss_key}" \
             --timeout 3600 --endpoint "$OSS_ENDPOINT_INTERNAL" 2>&1)
  dl_url=$(echo "$sign_out" | grep '^http' | head -1)
  if [ -z "$dl_url" ] || [ "${#dl_url}" -lt 50 ]; then
    echo "!!! signed URL 提取失败; sign output: $sign_out"
    rm -f "$tar_path"
    exit 1
  fi

  # 4. RunCommand: 服务器 curl + 解压 + 装依赖
  local script
  script=$(cat <<SHELL
set -e
mkdir -p $DEPLOY_DIR
cd $DEPLOY_DIR
echo "=== OSS 内网下载 ==="
curl -fsSL "$dl_url" -o /tmp/caiji-deploy.tar.gz
tar xzf /tmp/caiji-deploy.tar.gz -C $DEPLOY_DIR
rm /tmp/caiji-deploy.tar.gz
echo "=== 装/升依赖 ==="
apt-get install -y -qq python3-venv python3-pip
[ ! -d .venv ] && python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "✅ 代码同步 + 依赖装好"
.venv/bin/python -c "import fastapi; import uvicorn; import httpx; print('FastAPI', fastapi.__version__, 'uvicorn', uvicorn.__version__, 'httpx', httpx.__version__)"
ls -la server.py parser.py db.py sync.py 2>&1 | head -10
SHELL
  )
  invoke_remote "$script"

  # 5. 本地清理 tar (OSS 上保留, 可在 OSS console 配 lifecycle rule 7 天后自动删)
  rm -f "$tar_path"
}

# ============ Step 1.5: 同步 secret 到 /etc/caiji-mvp.env (chmod 600) ============
provision_secret() {
  echo "==> Step 1.5: 同步本地 .env → 服务器 $ENV_FILE_REMOTE (chmod 600)"
  if [ ! -f "$ENV_FILE_LOCAL" ]; then
    echo "!!! 本地 .env 不存在: $ENV_FILE_LOCAL"
    echo "    生成命令:"
    echo "      python3 -c 'import secrets; print(\"CAIJI_SECRET=\" + secrets.token_urlsafe(32))' > $ENV_FILE_LOCAL"
    exit 1
  fi
  local env_b64
  env_b64=$(base64 < "$ENV_FILE_LOCAL" | tr -d '\n')
  local script
  script=$(cat <<SHELL
set -e
echo "$env_b64" | base64 -d > $ENV_FILE_REMOTE
chmod 600 $ENV_FILE_REMOTE
chown root:root $ENV_FILE_REMOTE
echo "=== $ENV_FILE_REMOTE 已写入 (chmod 600) ==="
ls -la $ENV_FILE_REMOTE
# 只打印 key 名, 不打印 secret 值, 避免泄漏到日志
echo "=== 配置的环境变量 (value 已隐藏) ==="
grep -E '^[A-Z_]+=' $ENV_FILE_REMOTE | sed 's/=.*\$/=<hidden>/'
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
  # 注意 nginx 变量 \$host \$remote_addr 必须保留，shell 变量 $DOMAIN 展开
  local script
  script=$(cat <<SHELL
set -e
# 临时配置只监听 80 (certbot 会改造成 443)
# 用占位符 + sed 替换，避免 heredoc 引号陷阱
cat > /tmp/caiji.nginx.template <<'NGINX_EOF'
server {
    listen 80;
    server_name __DOMAIN__;
    location /onebot/event {
        proxy_pass http://127.0.0.1:8090/onebot/event;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        client_max_body_size 5M;
    }
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
NGINX_EOF
sed "s/__DOMAIN__/$DOMAIN/g" /tmp/caiji.nginx.template > $NGINX_CONF_REMOTE
ln -sf $NGINX_CONF_REMOTE /etc/nginx/sites-enabled/caiji
nginx -t
systemctl reload nginx
echo "--- 实际写入的配置（前 10 行）---"
head -10 $NGINX_CONF_REMOTE
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
  echo "==> Step 6: 验证 HTTPS / 服务 / 端到端 (含验签)"
  local script
  script=$(cat <<SHELL
set -e
. $ENV_FILE_REMOTE
echo "=== 服务状态 ==="
systemctl is-active caiji-mvp
echo "=== 端口监听 ==="
ss -tlnp | grep 8090
echo "=== HTTPS 健康检查 (GET /) ==="
curl -sS https://$DOMAIN/ | head -c 300
echo ""
echo "=== TLS handshake ==="
echo Q | openssl s_client -connect $DOMAIN:443 -servername $DOMAIN 2>&1 | grep -E 'subject=|issuer=|Protocol' | head -5

echo "=== 安全验签 1/2: 无 X-Signature, 预期 401 ==="
curl -sS -X POST https://$DOMAIN/onebot/event \\
  -H "Content-Type: application/json" \\
  -d '{"test":"no-sig-should-fail"}' \\
  -w "\\n[HTTP %{http_code}]\\n"

echo "=== 安全验签 2/2: 带正确 HMAC-SHA1 X-Signature, 预期 200 ==="
BODY='{"self_id":100,"user_id":1,"message_type":"group","sub_type":"normal","sender":{"nickname":"verify-bot"},"raw_message":"验证 9.9元 (verifyToken) HU9999","post_type":"message","group_id":999,"group_name":"verify-group"}'
SIG="sha1=\$(printf '%s' "\$BODY" | openssl dgst -sha1 -hmac "\$CAIJI_SECRET" | awk '{print \$2}')"
curl -sS -X POST https://$DOMAIN/onebot/event \\
  -H "Content-Type: application/json" \\
  -H "X-Signature: \$SIG" \\
  -d "\$BODY" \\
  -w "\\n[HTTP %{http_code}]\\n"

echo "=== 最近 5 条采集 ==="
curl -sS https://$DOMAIN/recent?limit=5 | head -c 600
echo ""
echo "=== 续期机制 ==="
systemctl is-enabled certbot.timer
SHELL
  )
  invoke_remote "$script"
}

# ============ Helper: 远程执行 ============
# 修复: 完整状态枚举 (Finished/Failed/Cancelled/Timeout/PartialFailed) 都退出 polling,
# 否则 Failed 时旧版死循环 polling 直到外层 Bash 超时, 不显示真实错误.
invoke_remote() {
  local script="$1"
  local b64 invoke_id status exit_code
  b64=$(printf '%s' "$script" | base64)
  invoke_id=$(aliyun ecs RunCommand --RegionId "$REGION" \
    --InstanceId.1 "$INSTANCE_ID" \
    --Type RunShellScript \
    --ContentEncoding Base64 \
    --CommandContent "$b64" \
    --Timeout 900 \
    --cli-query 'InvokeId' | tr -d '"')
  echo "InvokeId=$invoke_id"
  status=""
  for i in $(seq 1 300); do
    status=$(aliyun ecs DescribeInvocations --RegionId "$REGION" --InvokeId "$invoke_id" --cli-query 'Invocations.Invocation[0].InvokeStatus' | tr -d '"')
    case "$status" in
      Finished|Failed|Cancelled|Timeout|PartialFailed) break ;;
    esac
    sleep 3
  done
  echo "[remote status=$status]"
  aliyun ecs DescribeInvocationResults --RegionId "$REGION" --InvokeId "$invoke_id" \
    --cli-query 'Invocation.InvocationResults.InvocationResult[0].Output' \
    | tr -d '"' | base64 -d
  exit_code=$(aliyun ecs DescribeInvocationResults --RegionId "$REGION" --InvokeId "$invoke_id" \
    --cli-query 'Invocation.InvocationResults.InvocationResult[0].ExitCode')
  if [ "$exit_code" != "0" ] || [ "$status" != "Finished" ]; then
    echo "!!! ExitCode=$exit_code Status=$status, aborting"
    exit 1
  fi
}

# ============ Main ============
case "${1:-all}" in
  code)     deploy_code_and_deps ;;
  deps)     deploy_code_and_deps ;;
  secret)   provision_secret ;;
  service)  setup_systemd ;;
  nginx)    setup_nginx ;;
  cert)     provision_cert ;;
  hardened) deploy_hardened_nginx ;;
  verify)   verify ;;
  all)
    deploy_code_and_deps
    provision_secret
    setup_systemd
    setup_nginx
    provision_cert
    deploy_hardened_nginx
    verify
    ;;
  *)
    echo "Usage: $0 [code|deps|secret|service|nginx|cert|hardened|verify|all]"
    exit 1
    ;;
esac

echo "==> Done. https://$DOMAIN ready."
