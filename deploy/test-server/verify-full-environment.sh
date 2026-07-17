#!/usr/bin/env bash
set -euo pipefail

: "${PUBLIC_BASE_URL:?set PUBLIC_BASE_URL to the externally reachable site origin}"
AUTH_INTERNAL_URL="${AUTH_INTERNAL_URL:-http://127.0.0.1:8095}"
: "${ADMIN_ALLOWED_SOURCE:?set ADMIN_ALLOWED_SOURCE to the exact deployed allowlisted IP or CIDR}"

PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
AUTH_INTERNAL_URL="${AUTH_INTERNAL_URL%/}"
ADMIN_ALLOWLIST_FILE="${ADMIN_ALLOWLIST_FILE:-/etc/nginx/snippets/huangque-admin-allowlist.conf}"
CONTENT_INTERNAL_URL="${CONTENT_INTERNAL_URL:-http://127.0.0.1:8096}"
IMGGEN_INTERNAL_URL="${IMGGEN_INTERNAL_URL:-http://127.0.0.1:8101}"
LEADGEN_INTERNAL_URL="${LEADGEN_INTERNAL_URL:-http://127.0.0.1:8100}"
DL_INTERNAL_URL="${DL_INTERNAL_URL:-http://127.0.0.1:8097}"

umask 077
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
probe_number=0

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

fetch_success() {
    label="$1"
    url="$2"
    probe_number=$((probe_number + 1))
    body_file="${TEMP_DIR}/body-${probe_number}"
    header_file="${TEMP_DIR}/headers-${probe_number}"
    if ! curl --fail-with-body --silent --show-error \
        --output "$body_file" --dump-header "$header_file" "$url"; then
        fail "${label} request failed"
    fi
    printf 'CHECK: %s -> success\n' "$label"
}

expect_status() {
    label="$1"
    expected="$2"
    url="$3"
    shift 3
    probe_number=$((probe_number + 1))
    body_file="${TEMP_DIR}/body-${probe_number}"
    if ! status="$(curl --silent --show-error --output "$body_file" \
        --write-out '%{http_code}' "$@" "$url")"; then
        fail "${label} request failed before an HTTP status was received"
    fi
    printf 'CHECK: %s -> %s\n' "$label" "$status"
    test "$status" = "$expected" || fail "${label} expected ${expected}, received ${status}"
}

require_header() {
    header_file="$1"
    header_name="$2"
    grep -Eqi "^${header_name}:[[:space:]]*[^[:space:]]" "$header_file" \
        || fail "required response header missing: ${header_name}"
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

test -f "$ADMIN_ALLOWLIST_FILE" || fail "admin allowlist snippet missing"
awk -v expected="$ADMIN_ALLOWED_SOURCE" '
    /^[[:space:]]*#/ { next }
    $1 == "allow" && $2 == expected ";" { found = 1 }
    END { exit(found ? 0 : 1) }
' "$ADMIN_ALLOWLIST_FILE" || fail "expected admin allowlist source is not active"
awk '
    {
        sub(/#.*/, "")
        gsub(/^[[:space:]]+|[[:space:]]+$/, "")
        if (length($0)) last = $0
    }
    END { exit(last == "deny all;" ? 0 : 1) }
' "$ADMIN_ALLOWLIST_FILE" || fail "admin allowlist must end with deny all"

ss -ltn | grep -q '127.0.0.1:10809' || fail "xray loopback port missing"
for port in 8095 8096 8097 8098 8100 8101; do
    ss -ltn | grep -q "127.0.0.1:${port}" || fail "backend loopback port missing: ${port}"
    if ss -ltn | grep -Eq "(0\.0\.0\.0|\[::\]):${port}"; then
        fail "backend port exposed publicly: ${port}"
    fi
done

fetch_success "internal auth health" "${AUTH_INTERNAL_URL}/api/auth/health"
auth_body="${TEMP_DIR}/body-${probe_number}"
grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$auth_body" || fail "auth health failed"

fetch_success "internal content health" "${CONTENT_INTERNAL_URL}/api/gen/health"
content_body="${TEMP_DIR}/body-${probe_number}"
grep -Eq '"has_openai"[[:space:]]*:[[:space:]]*true' "$content_body" || fail "OpenAI key not loaded"
grep -Eq '"has_tikhub"[[:space:]]*:[[:space:]]*true' "$content_body" || fail "TikHub key not loaded"

fetch_success "internal image health" "${IMGGEN_INTERNAL_URL}/api/gen/banana/health"
fetch_success "internal lead generation health" "${LEADGEN_INTERNAL_URL}/api/gen/leadgen/health"
fetch_success "internal download health" "${DL_INTERNAL_URL}/api/gen/dl/health"

fetch_success "public login /login.html" "${PUBLIC_BASE_URL}/login.html"
login_headers="${TEMP_DIR}/headers-${probe_number}"
require_header "$login_headers" "X-Content-Type-Options"
grep -Eqi '^X-Content-Type-Options:[[:space:]]*nosniff([[:space:]]|$)' "$login_headers" \
    || fail "X-Content-Type-Options must be nosniff"
require_header "$login_headers" "Referrer-Policy"
require_header "$login_headers" "Permissions-Policy"
require_header "$login_headers" "Content-Security-Policy"
grep -Eqi "^Content-Security-Policy:.*frame-ancestors[[:space:]]+'self'" "$login_headers" \
    || fail "Content-Security-Policy must restrict frame ancestors"
case "$PUBLIC_BASE_URL" in
    https://*) require_header "$login_headers" "Strict-Transport-Security" ;;
    *) printf 'CHECK: Strict-Transport-Security -> not applicable to non-HTTPS URL\n' ;;
esac

internal_paths=(
    /api/auth/points/deduct
    /api/auth/points/refund
    /api/auth/admin/points/adjust
    /api/auth/admin/points/audit
    /api/auth/admin/users
    /api/auth/admin/recharge/review
    /api/auth/admin/recharge/orders
)
for path in "${internal_paths[@]}"; do
    expect_status "public boundary ${path}" 404 "${PUBLIC_BASE_URL}${path}"
    expect_status "spoofed internal token ${path}" 404 "${PUBLIC_BASE_URL}${path}" \
        --header 'X-HQ-Internal-Token: spoofed-public-client-value'
done

# Run these probes from a source that is not in the deployed admin allowlist.
expect_status "non-allowlisted admin page /admin/" 403 "${PUBLIC_BASE_URL}/admin/"
expect_status "non-allowlisted admin API /api/admin/" 403 "${PUBLIC_BASE_URL}/api/admin/"

proxy_code="$(curl --proxy http://127.0.0.1:10809 --silent --output /dev/null --write-out '%{http_code}' --max-time 15 https://api.openai.com/v1/models)"
case "$proxy_code" in
    401|403) ;;
    *) fail "unexpected no-auth OpenAI connectivity status: ${proxy_code}" ;;
esac

cd /opt/huangque-test-server
git_status="$(git -c safe.directory=/opt/huangque-test-server status --short)" || fail "git status failed"
printf '%s\n' "$git_status" | grep -Eq '(providers\.env|xray-client\.json)' && fail "secret path appears in Git status"

printf 'PASS: full test environment configuration and no-cost connectivity checks\n'
