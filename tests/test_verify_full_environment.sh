#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="${ROOT}/deploy/test-server/verify-full-environment.sh"
HARNESS_TEMP="$(mktemp -d)"
trap 'rm -rf "$HARNESS_TEMP"' EXIT

mkdir -p "${HARNESS_TEMP}/bin" "${HARNESS_TEMP}/probe-tmp"

cat >"${HARNESS_TEMP}/bin/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
set -euo pipefail

output_file=""
header_file=""
write_status=0
url=""
fail_with_body=0
while (($#)); do
    case "$1" in
        --output|--dump-header|--write-out|--connect-timeout|--max-time|--header|--proxy)
            option="$1"
            value="$2"
            shift 2
            case "$option" in
                --output) output_file="$value" ;;
                --dump-header) header_file="$value" ;;
                --write-out) write_status=1 ;;
            esac
            ;;
        --fail-with-body) fail_with_body=1; shift ;;
        --silent|--show-error) shift ;;
        --) shift; url="$1"; shift ;;
        *) printf 'unexpected fake curl argument: %s\n' "$1" >&2; exit 64 ;;
    esac
done

status="${FAKE_CURL_STATUS:-204}"
if [[ "${FAKE_CURL_MODE:-}" == "paired" ]]; then
    case "$url" in
        */admin/|*/api/admin/) status=403 ;;
        *) status=404 ;;
    esac
fi
printf '%s' 'super-secret-body' >"$output_file"
if [[ -n "$header_file" ]]; then
    printf '%s\n' \
        'HTTP/1.1 204 No Content' \
        'Set-Cookie: session-secret' \
        'X-CSRF-Token: csrf-secret' >"$header_file"
fi
if ((write_status)); then
    printf '%s' "$status"
fi
if ((fail_with_body)) && [[ "$status" =~ ^[45] ]]; then
    exit 22
fi
FAKE_CURL
chmod +x "${HARNESS_TEMP}/bin/curl"

run_case() {
    local name="$1"
    local expected_exit="$2"
    local mode="$3"
    local status="$4"
    local output="${HARNESS_TEMP}/${name}.out"
    local actual_exit

    set +e
    PATH="${HARNESS_TEMP}/bin:${PATH}" \
        TMPDIR="${HARNESS_TEMP}/probe-tmp" \
        PUBLIC_BASE_URL="https://example.invalid" \
        AUTH_INTERNAL_URL="http://127.0.0.1:8095" \
        ADMIN_ALLOWED_SOURCE="192.0.2.10/32" \
        FAKE_CURL_MODE="$mode" \
        FAKE_CURL_STATUS="$status" \
        PROBE_CASE="$name" \
        SCRIPT_PATH="$SCRIPT_PATH" \
        bash -c '
            source <(sed -n "1,/^# END TESTABLE PROBE HELPERS$/p" "$SCRIPT_PATH")
            case "$PROBE_CASE" in
                2xx) fetch_success "2xx health" "https://example.invalid/health" ;;
                redirect) fetch_success "redirect health" "https://example.invalid/health" ;;
                paired)
                    expect_status "plain internal path" 404 "https://example.invalid/api/auth/points/deduct"
                    expect_status "spoofed internal path" 404 "https://example.invalid/api/auth/points/deduct" --header "X-HQ-Internal-Token: fake"
                    expect_status "non-allowlisted admin" 403 "https://example.invalid/admin/"
                    ;;
            esac
        ' >"$output" 2>&1
    actual_exit=$?
    set -e

    [[ "$actual_exit" == "$expected_exit" ]] || {
        printf 'case %s: expected exit %s, got %s\n' "$name" "$expected_exit" "$actual_exit" >&2
        return 1
    }
    if grep -Eq 'super-secret-body|session-secret|csrf-secret' "$output"; then
        printf 'case %s leaked a fake secret\n' "$name" >&2
        return 1
    fi
}

run_case 2xx 0 "" 204
run_case redirect 1 "" 302
run_case paired 0 paired 200

run_allowlist_case() {
    local name="$1"
    local expected_exit="$2"
    local expected_source="$3"
    local contents="$4"
    local config="${HARNESS_TEMP}/${name}.conf"
    local output="${HARNESS_TEMP}/${name}-allowlist.out"
    local actual_exit
    printf '%s\n' "$contents" >"$config"
    set +e
    SCRIPT_PATH="$SCRIPT_PATH" VERIFY_ADMIN_ALLOWLIST_PATH="${ROOT}/deploy/test-server/verify-admin-allowlist.py" CONFIG_PATH="$config" EXPECTED_SOURCE="$expected_source" bash -c '
        source <(sed -n "1,/^# END TESTABLE PROBE HELPERS$/p" "$SCRIPT_PATH")
        verify_admin_allowlist "$CONFIG_PATH" "$EXPECTED_SOURCE"
    ' >"$output" 2>&1
    actual_exit=$?
    set -e
    [[ "$actual_exit" == "$expected_exit" ]] || {
        printf 'allowlist case %s: expected exit %s, got %s\n' "$name" "$expected_exit" "$actual_exit" >&2
        return 1
    }
    if grep -Fq "$expected_source" "$output"; then
        printf 'allowlist case %s leaked the configured source\n' "$name" >&2
        return 1
    fi
}

run_allowlist_case env-all 1 all $'allow 192.0.2.10/32;\ndeny all;'
run_allowlist_case allow-all 1 192.0.2.10/32 $'allow all;\ndeny all;'
run_allowlist_case ipv4-universal 1 192.0.2.10/32 $'allow 0.0.0.0/0;\nallow 192.0.2.10/32;\ndeny all;'
run_allowlist_case ipv6-universal 1 2001:db8::10/128 $'allow ::/0;\nallow 2001:db8::10/128;\ndeny all;'
run_allowlist_case early-deny 1 192.0.2.10/32 $'deny all;\nallow 192.0.2.10/32;\ndeny all;'
run_allowlist_case unexpected 1 192.0.2.10/32 $'allow 192.0.2.10/32;\nsatisfy any;\ndeny all;'
run_allowlist_case malformed 1 192.0.2.10/32 $'allow 999.0.2.10/33;\ndeny all;'
run_allowlist_case missing-expected 1 192.0.2.10/32 $'allow 198.51.100.7/32;\ndeny all;'
run_allowlist_case valid-ipv4 0 192.0.2.10 $'# managed\nallow 192.0.2.10/32;\nallow 198.51.100.0/24;\ndeny all;'
run_allowlist_case valid-ipv6 0 2001:db8::10 $'allow 2001:db8::10/128;\nallow 2001:db8:1::/64;\ndeny all;'

if find "${HARNESS_TEMP}/probe-tmp" -mindepth 1 -print -quit | grep -q .; then
    printf 'cleanup failed: probe temp directory is not empty\n' >&2
    exit 1
fi

printf 'PASS: fake-curl verification helper harness\n'
