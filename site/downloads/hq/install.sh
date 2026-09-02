#!/bin/sh
set -eu

version="0.15.2"
wheel_name="huangque_hq_cli-0.15.2-py3-none-any.whl"
wheel_size="64405"
wheel_sha256="5b46bddc58219f09c873aa36fc5eec8f1599fac7699a47679d7ff5cf0021e7f0"
wheel_url="https://huangquechuanmei.com/downloads/hq/v0.15.2/$wheel_name"

fail() { printf 'HQ CLI 安装失败：%s\n' "$1" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || fail "需要 curl"
python_bin=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    python_bin="$(command -v "$candidate")"
    break
  fi
done
[ -n "$python_bin" ] || fail "需要 Python 3.10 或更高版本"
[ -n "${HOME:-}" ] || fail "HOME 未设置"

data_root="${XDG_DATA_HOME:-$HOME/.local/share}/hq-cli"
bin_root="$HOME/.local/bin"
target_dir="$data_root/$version"
link_path="$bin_root/hq"
mkdir -p "$data_root" "$bin_root"

download_dir="$(mktemp -d "${TMPDIR:-/tmp}/hq-cli-download.XXXXXX")"
stage_dir=""
cleanup() {
  [ -z "$stage_dir" ] || [ ! -d "$stage_dir" ] || rm -rf "$stage_dir"
  [ ! -d "$download_dir" ] || rm -rf "$download_dir"
}
trap cleanup EXIT HUP INT TERM

wheel_path="$download_dir/$wheel_name"
curl --proto '=https' --tlsv1.2 -fsSL "$wheel_url" -o "$wheel_path"
actual_size="$(wc -c < "$wheel_path" | tr -d '[:space:]')"
[ "$actual_size" = "$wheel_size" ] || fail "安装包大小校验失败"
if command -v shasum >/dev/null 2>&1; then
  actual_sha256="$(shasum -a 256 "$wheel_path" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  actual_sha256="$(sha256sum "$wheel_path" | awk '{print $1}')"
else
  fail "需要 shasum 或 sha256sum 校验安装包"
fi
[ "$actual_sha256" = "$wheel_sha256" ] || fail "安装包 SHA-256 校验失败"

if [ ! -x "$target_dir/venv/bin/hq" ]; then
  [ ! -e "$target_dir" ] || fail "$target_dir 已存在但不是完整安装，请先人工检查"
  stage_dir="$(mktemp -d "$data_root/.hq-cli-$version.XXXXXX")"
  "$python_bin" -m venv "$stage_dir/venv"
  "$stage_dir/venv/bin/python" -m pip install --disable-pip-version-check --no-index --no-deps "$wheel_path" >/dev/null
  "$stage_dir/venv/bin/hq" version --json >/dev/null
  mv "$stage_dir" "$target_dir"
  stage_dir=""
fi

# venv 控制台脚本会记住临时目录；移动后从最终路径重装一次以刷新 shebang。
if ! "$target_dir/venv/bin/hq" version --json >/dev/null 2>&1; then
  "$target_dir/venv/bin/python" -m pip install --disable-pip-version-check --no-index --no-deps --force-reinstall "$wheel_path" >/dev/null
fi
"$target_dir/venv/bin/hq" version --json >/dev/null || fail "安装后的 hq 无法启动"

if [ -L "$link_path" ]; then
  current_target="$(readlink "$link_path")"
  case "$current_target" in
    "$data_root"/*/venv/bin/hq) ;;
    *) fail "$link_path 已属于其他程序，未覆盖" ;;
  esac
elif [ -e "$link_path" ]; then
  fail "$link_path 已存在且不是符号链接，未覆盖"
fi
ln -sfn "$target_dir/venv/bin/hq" "$link_path"

printf 'HQ CLI %s 已安装：%s\n' "$version" "$link_path"
case ":${PATH:-}:" in
  *":$bin_root:"*) ;;
  *) printf '请把 %s 加入 PATH，然后运行：hq login --json\n' "$bin_root" ;;
esac
