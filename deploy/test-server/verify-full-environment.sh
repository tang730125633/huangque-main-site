#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

test -f /etc/huangque-test/providers.env || fail "providers.env missing"
test "$(stat -c '%a' /etc/huangque-test/providers.env)" = "600" || fail "providers.env mode is not 600"
test "$(stat -c '%U:%G' /etc/huangque-test/providers.env)" = "root:root" || fail "providers.env owner is not root:root"

for key in GEMINI_API_KEY OPENAI_API_KEY COS_SECRET_ID COS_SECRET_KEY COS_BUCKET TIKHUB_KEY RUNNINGHUB_API_KEY; do
    grep -q "^${key}=" /etc/huangque-test/providers.env || fail "required provider key name missing: ${key}"
done

test -f /etc/huangque-test/egress/xray-client.json || fail "xray config missing"
test "$(stat -c '%a' /etc/huangque-test/egress/xray-client.json)" = "640" || fail "xray config mode is not 640"

for service in nginx huangque-test-egress huangque-test-auth huangque-test-content huangque-test-admin huangque-test-imggen huangque-test-leadgen huangque-test-dl; do
    systemctl is-active --quiet "$service" || fail "inactive service: ${service}"
    systemctl is-enabled --quiet "$service" || fail "disabled service: ${service}"
done

nginx -t >/dev/null 2>&1 || fail "nginx configuration invalid"

ss -ltn | grep -q '127.0.0.1:10809' || fail "xray loopback port missing"
for port in 8095 8096 8097 8098 8100 8101; do
    ss -ltn | grep -q "127.0.0.1:${port}" || fail "backend loopback port missing: ${port}"
    if ss -ltn | grep -Eq "(0\.0\.0\.0|\[::\]):${port}"; then
        fail "backend port exposed publicly: ${port}"
    fi
done

auth_json="$(curl --fail --silent --show-error http://127.0.0.1/api/auth/health)"
content_json="$(curl --fail --silent --show-error http://127.0.0.1/api/gen/health)"
printf '%s' "$auth_json" | grep -q '"ok": true' || fail "auth health failed"
printf '%s' "$content_json" | grep -q '"has_openai": true' || fail "OpenAI key not loaded"
printf '%s' "$content_json" | grep -q '"has_tikhub": true' || fail "TikHub key not loaded"

proxy_code="$(curl --proxy http://127.0.0.1:10809 --silent --output /dev/null --write-out '%{http_code}' --max-time 15 https://api.openai.com/v1/models)"
case "$proxy_code" in
    401|403) ;;
    *) fail "unexpected no-auth OpenAI connectivity status: ${proxy_code}" ;;
esac

cd /opt/huangque-test-server
git_status="$(git -c safe.directory=/opt/huangque-test-server status --short)" || fail "git status failed"
printf '%s\n' "$git_status" | grep -Eq '(providers\.env|xray-client\.json)' && fail "secret path appears in Git status"

printf 'PASS: full test environment configuration and no-cost connectivity checks\n'
