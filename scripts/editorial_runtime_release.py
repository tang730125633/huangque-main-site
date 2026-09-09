#!/usr/bin/env python3
"""口播网感模板专用事务发布器。

只安装「口播网感模板」运行闭环所需的固定白名单内容：
  1. 共享 editorial_contract.py → auth-service 与 content-api 两处
  2. 隔离 HyperFrames 0.8.33 运行时（npm ci，锁定版本，不影响其它运行时）
  3. 口播网感模板静态资产（字体 / gsap / template.json）
  4. 锁定浏览器的 systemd drop-in

事务语义（固定白名单，绝不越界）：
  preimage  == 本次发布前服务器的已知状态（本轮全部为「不存在」）
  postimage == 本次发布后的目标状态（由仓库内容计算）
  当前 == preimage  → needs_install
  当前 == postimage → already_postimage（零写入、零重启）
  其他             → drifted（在备份/写入/npm/重启之前立即拒绝）

只重启 huangque-auth → huangque-content。任一步骤失败完整回滚。
绝不触碰：模板业务代码、数据库、点数、用户数据、付费供应商。

用法：
  python scripts/editorial_runtime_release.py --check       # 只读预检（本地校验白名单）
  python scripts/editorial_runtime_release.py --dry-run     # 计算分类与动作，不写
  python scripts/editorial_runtime_release.py --execute     # 事务安装（生产，需确认）
"""
import argparse
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 固定白名单（唯一允许写入的目标）
# ---------------------------------------------------------------------------
HYPERFRAMES_VERSION = "0.8.33"
RUNTIME_ROOT = "/opt/huangque/editorial-hyperframes-0.8.33"
BROWSER = "/snap/bin/chromium"
AUTH_SERVICE_ROOT = "/home/ubuntu/auth-service"
CONTENT_SERVICE_ROOT = "/home/ubuntu/content-api"
WEBROOT = "/var/www/huangquechuanmei"

FILE = "file"
TREE = "tree"
NPM = "npm"

CONTRACT_SOURCE = "server/content_domains/editorial_contract.py"
AUTH_CONTRACT = AUTH_SERVICE_ROOT + "/content_domains/editorial_contract.py"
CONTENT_CONTRACT = CONTENT_SERVICE_ROOT + "/content_domains/editorial_contract.py"
TEMPLATE_SOURCE = "site/assets/one-click/templates/ip-editorial-serif-v1"
TEMPLATE_DEST = WEBROOT + "/assets/one-click/templates/ip-editorial-serif-v1"
NPM_SOURCE = "tools/hyperframes/ip-editorial-serif-v1"
DROPIN_SOURCE = "deploy/systemd/huangque-content.service.d/editorial-runtime.conf"
DROPIN_DEST = "/etc/systemd/system/huangque-content.service.d/editorial-runtime.conf"

# (kind, repo_source, server_dest)。顺序即安装顺序。
ARTIFACTS = (
    (FILE, CONTRACT_SOURCE, AUTH_CONTRACT),
    (FILE, CONTRACT_SOURCE, CONTENT_CONTRACT),
    (TREE, TEMPLATE_SOURCE, TEMPLATE_DEST),
    (NPM, NPM_SOURCE, RUNTIME_ROOT),
    (FILE, DROPIN_SOURCE, DROPIN_DEST),
)

# 只允许重启这两个服务，且顺序固定：auth 先，content 后。
RESTART_SERVICES = ("huangque-auth", "huangque-content")

HEALTH_URL = "https://huangquechuanmei.com/api/gen/health"
UNAUTH_URL = "https://huangquechuanmei.com/api/gen/matrix-template/templates"

ABSENT = {"absent": True}
PREIMAGE = ABSENT  # 本轮所有目标发布前均为「不存在」


class ReleaseError(RuntimeError):
    pass


class Drifted(ReleaseError):
    pass


# ---------------------------------------------------------------------------
# 纯函数：哈希、postimage、分类、白名单校验、计划
# ---------------------------------------------------------------------------
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_of_bytes(data):
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def _iter_tree_files(root):
    root = pathlib.Path(root)
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path.relative_to(root).as_posix(), path


def file_postimage(repo_root, source):
    path = repo_root / source
    if not path.is_file():
        raise ReleaseError("白名单源文件缺失: %s" % source)
    return {"sha256": sha256_file(path)}


def tree_postimage(repo_root, source):
    root = repo_root / source
    if not root.is_dir():
        raise ReleaseError("白名单源目录缺失: %s" % source)
    files = {rel: sha256_file(path) for rel, path in _iter_tree_files(root)}
    if not files:
        raise ReleaseError("白名单源目录为空: %s" % source)
    return {"files": files}


def npm_postimage():
    return {"hyperframes_version": HYPERFRAMES_VERSION}


def postimage_for(repo_root, kind, source):
    if kind == FILE:
        return file_postimage(repo_root, source)
    if kind == TREE:
        return tree_postimage(repo_root, source)
    if kind == NPM:
        return npm_postimage()
    raise ReleaseError("未知产物类型: %r" % (kind,))


def content_fingerprint(state):
    """把远端完整状态（含 exists/mode/uid/gid）归一化为纯内容指纹。"""
    if not state or not state.get("exists"):
        return ABSENT
    if state.get("symlink"):
        return {"symlink": True}
    if "sha256" in state:
        return {"sha256": state["sha256"]}
    if "hyperframes_version" in state:
        return {"hyperframes_version": state["hyperframes_version"]}
    if "files" in state:
        return {"files": state["files"]}
    return {"unreadable": True}


def classify(current, preimage, postimage):
    """当前状态分类：needs_install / already_postimage / drifted。"""
    if current == preimage:
        return "needs_install"
    if current == postimage:
        return "already_postimage"
    return "drifted"


def _assert_within_root(root, path):
    root = pathlib.Path(root).resolve()
    path = pathlib.Path(path)
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise ReleaseError("白名单路径逃逸: %s" % path)


def validate_whitelist(repo_root):
    """固定白名单只允许仓库内普通文件/目录；拒绝符号链接、路径逃逸、非常规文件。"""
    repo_root = pathlib.Path(repo_root).resolve()
    for kind, source, dest in ARTIFACTS:
        if not isinstance(source, str) or not source:
            raise ReleaseError("白名单源为空")
        if not isinstance(dest, str) or not dest.startswith("/"):
            raise ReleaseError("白名单目标必须是绝对路径: %r" % (dest,))
        if "\x00" in source or "\x00" in dest:
            raise ReleaseError("白名单路径含 NUL 字节")
        src = repo_root / source
        _assert_within_root(repo_root, src)
        if kind == FILE:
            if src.is_symlink() or not src.is_file():
                raise ReleaseError("白名单要求普通文件: %s" % source)
        elif kind in (TREE, NPM):
            if src.is_symlink() or not src.is_dir():
                raise ReleaseError("白名单要求普通目录: %s" % source)
            for rel, path in _iter_tree_files(src):
                if path.is_symlink():
                    raise ReleaseError("白名单目录含符号链接: %s" % rel)
        else:
            raise ReleaseError("未知产物类型: %r" % (kind,))


def build_plan(repo_root, current):
    """current: {server_dest: 指纹}。返回按安装顺序的动作列表。"""
    validate_whitelist(repo_root)
    plan = []
    for kind, source, dest in ARTIFACTS:
        postimage = postimage_for(repo_root, kind, source)
        current_fp = content_fingerprint(current.get(dest))
        state = classify(current_fp, PREIMAGE, postimage)
        plan.append({"kind": kind, "source": source, "dest": dest,
                     "state": state, "postimage": postimage})
    return plan


# ---------------------------------------------------------------------------
# Remote：SSH 执行抽象（生产用 subprocess ssh/scp；测试用 FakeRemote 替换）
# ---------------------------------------------------------------------------
def _py(value):
    return shlex.quote(str(value))


_REMOTE_STATE_SCRIPT = (
    "import json,os,hashlib,stat as s\n"
    "p=%(dest)s\n"
    "def h(f):\n"
    "  d=hashlib.sha256()\n"
    "  with open(f,'rb') as fh:\n"
    "    for c in iter(lambda: fh.read(1<<20), b''): d.update(c)\n"
    "  return d.hexdigest()\n"
    "def blob(f):\n"
    "  data=open(f,'rb').read()\n"
    "  return hashlib.sha1(b'blob %%d\\x00'%%len(data)+data).hexdigest()\n"
    "out={'exists':False}\n"
    "try:\n"
    "  st=os.lstat(p)\n"
    "except OSError:\n"
    "  st=None\n"
    "if st is not None:\n"
    "  out['exists']=True\n"
    "  out['mode']=oct(s.S_IMODE(st.st_mode))\n"
    "  out['uid']=st.st_uid\n"
    "  out['gid']=st.st_gid\n"
    "  if s.S_ISLNK(st.st_mode):\n"
    "    out['symlink']=True\n"
    "  elif s.S_ISREG(st.st_mode):\n"
    "    out['sha256']=h(p)\n"
    "    out['git_blob']=blob(p)\n"
    "  elif s.S_ISDIR(st.st_mode):\n"
    "    if %(kind)s=='npm':\n"
    "      try:\n"
    "        import pathlib\n"
    "        pj=pathlib.Path(p)/'node_modules/hyperframes/package.json'\n"
    "        out['hyperframes_version']=json.loads(pj.read_text())['version']\n"
    "      except Exception:\n"
    "        out['unreadable']=True\n"
    "    else:\n"
    "      files={}\n"
    "      for r,_,fs in os.walk(p):\n"
    "        for f in fs:\n"
    "          fp=os.path.join(r,f)\n"
    "          files[os.path.relpath(fp,p)]=h(fp)\n"
    "      out['files']=files\n"
    "print(json.dumps(out))"
)


class Remote:
    """子进程版 SSH 执行器。方法保持小而稳定，便于测试 mock。"""

    def __init__(self, host, user="ubuntu", ssh_opts=()):
        self.host = host
        self.user = user
        self.ssh_opts = tuple(ssh_opts)
        self.target = user + "@" + host

    def _ssh_base(self):
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                *self.ssh_opts, self.target]

    def run(self, command):
        return subprocess.run(self._ssh_base() + [command],
                              capture_output=True, text=True, timeout=900)

    def run_checked(self, command, label):
        result = self.run(command)
        if result.returncode != 0:
            raise ReleaseError("%s 失败: %s" % (label, (result.stderr or "").strip()[:300]))
        return result

    def put_file(self, local, remote):
        local = pathlib.Path(local)
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes",
                        *self.ssh_opts, str(local), self.target + ":" + remote],
                       check=True, timeout=600)

    def put_dir(self, local, remote):
        local = pathlib.Path(local)
        subprocess.run(["scp", "-q", "-r", "-o", "BatchMode=yes",
                        *self.ssh_opts, str(local), self.target + ":" + remote],
                       check=True, timeout=900)

    def read_state(self, dest, kind):
        code = _REMOTE_STATE_SCRIPT % {"dest": json.dumps(dest), "kind": json.dumps(kind)}
        result = self.run("python3 -c " + _py(code))
        if result.returncode != 0:
            raise ReleaseError("读取远端状态失败: %s" % (result.stderr or "").strip()[:300])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ReleaseError("远端状态输出无效")


# ---------------------------------------------------------------------------
# 事务编排：备份 → 安装 → 校验 → 重启 → 验收；失败完整回滚
# ---------------------------------------------------------------------------
class Transaction:
    def __init__(self, remote, repo_root=ROOT, health_timeout=60, health_interval=5):
        self.remote = remote
        self.repo_root = pathlib.Path(repo_root)
        self.installed = []        # 已写入的目标（回滚时按逆序删除/恢复）
        self.preimage_states = {}  # 事务开始时的真实状态（回滚依据）
        self.events = []           # 事务日志
        self.health_timeout = health_timeout
        self.health_interval = health_interval
        self.backup_dir = "/tmp/.editorial-release-backup"

    def log(self, message):
        self.events.append(message)

    # -- 远端状态 ---------------------------------------------------------
    def current_states(self):
        return {dest: self.remote.read_state(dest, kind)
                for kind, _, dest in ARTIFACTS}

    # -- 备份（发布前保存服务器真实状态） ---------------------------------
    def backup(self, plan):
        """发布前保存服务器真实状态：字节、是否存在、SHA-256、git blob、mode、uid/gid。"""
        self.remote.run_checked("mkdir -p %s" % _py(self.backup_dir), "创建备份目录")
        manifest = {"artifacts": []}
        for item in plan:
            state = self.remote.read_state(item["dest"], item["kind"])
            entry = {"dest": item["dest"], "kind": item["kind"],
                     "existed": bool(state.get("exists")),
                     "mode": state.get("mode"), "uid": state.get("uid"),
                     "gid": state.get("gid"), "sha256": state.get("sha256"),
                     "git_blob": state.get("git_blob"),
                     "symlink": bool(state.get("symlink"))}
            if state.get("exists") and not state.get("symlink"):
                if item["kind"] == FILE and state.get("sha256"):
                    self.remote.run_checked(
                        "sudo cp -a --parents %s %s/"
                        % (_py(item["dest"]), _py(self.backup_dir)),
                        "备份已有内容 " + item["dest"])
                    entry["bytes_backed_up"] = True
                elif item["kind"] in (TREE, NPM):
                    safe = item["dest"].lstrip("/").replace("/", "_")
                    self.remote.run_checked(
                        "sudo tar -cf %s/%s.tar -C / %s"
                        % (_py(self.backup_dir), _py(safe), _py(item["dest"].lstrip("/"))),
                        "备份已有目录 " + item["dest"])
                    entry["backup_tar"] = safe + ".tar"
            manifest["artifacts"].append(entry)
        manifest_path = _write_temp_json(manifest)
        try:
            self.remote.put_file(manifest_path, self.backup_dir + "/manifest.json")
        finally:
            try:
                os.unlink(manifest_path)
            except OSError:
                pass
        return manifest

    # -- 安装（每步写文件都记录到 self.installed） -------------------------
    def install_files(self, plan):
        for item in plan:
            if (item["state"] == "needs_install" and item["kind"] == FILE
                    and item["dest"] != DROPIN_DEST):
                self._install_one_file(item)

    def _install_one_file(self, item):
        dest = item["dest"]
        if dest == DROPIN_DEST:
            self.remote.run_checked(
                "sudo mkdir -p %s" % _py(pathlib.Path(dest).parent.as_posix()),
                "创建 drop-in 目录")
        else:
            self.remote.run_checked(
                "sudo mkdir -p %s" % _py(pathlib.Path(dest).parent.as_posix()),
                "创建目录 " + dest)
        self.remote.put_file(self.repo_root / item["source"], "/tmp/.editorial-stage")
        self.remote.run_checked(
            "sudo install -m 0644 /tmp/.editorial-stage %s && rm -f /tmp/.editorial-stage"
            % _py(dest), "安装 " + dest)
        self.installed.append(dest)

    def install_tree(self, plan):
        for item in plan:
            if item["state"] == "needs_install" and item["kind"] == TREE:
                self.remote.run_checked(
                    "sudo mkdir -p %s" % _py(item["dest"]), "创建目录 " + item["dest"])
                self.remote.put_dir(self.repo_root / item["source"], "/tmp/.editorial-stage-tree")
                self.remote.run_checked(
                    "sudo cp -a /tmp/.editorial-stage-tree/. %s/ && sudo rm -rf /tmp/.editorial-stage-tree"
                    % _py(item["dest"]), "安装目录 " + item["dest"])
                self.installed.append(item["dest"])

    def install_npm(self, plan):
        for item in plan:
            if item["state"] == "needs_install" and item["kind"] == NPM:
                self.remote.run_checked(
                    "sudo mkdir -p %s && sudo chown ubuntu:ubuntu %s"
                    % (_py(RUNTIME_ROOT), _py(RUNTIME_ROOT)), "创建运行时目录")
                self.remote.put_file(self.repo_root / NPM_SOURCE / "package.json",
                                     "/tmp/.editorial-package.json")
                self.remote.put_file(self.repo_root / NPM_SOURCE / "package-lock.json",
                                     "/tmp/.editorial-package-lock.json")
                self.remote.run_checked(
                    "sudo cp /tmp/.editorial-package.json %s/package.json && "
                    "sudo cp /tmp/.editorial-package-lock.json %s/package-lock.json && "
                    "sudo chown ubuntu:ubuntu %s/package.json %s/package-lock.json && "
                    "rm -f /tmp/.editorial-package.json /tmp/.editorial-package-lock.json"
                    % (_py(RUNTIME_ROOT), _py(RUNTIME_ROOT), _py(RUNTIME_ROOT), _py(RUNTIME_ROOT)),
                    "落位 package 文件")
                self.remote.run_checked(
                    "cd %s && sudo -u ubuntu npm ci --no-audit --no-fund" % _py(RUNTIME_ROOT),
                    "npm ci 安装 HyperFrames")
                version_check = (
                    'import json,sys; '
                    'sys.exit(0 if json.load(open(%s)).get("version") == %s else 1)'
                    % (json.dumps(RUNTIME_ROOT + "/node_modules/hyperframes/package.json"),
                       json.dumps(HYPERFRAMES_VERSION)))
                self.remote.run_checked("python3 -c " + _py(version_check),
                                        "校验 HyperFrames 版本为 0.8.33")
                self.installed.append(RUNTIME_ROOT)

    def install_dropin(self, plan):
        for item in plan:
            if item["state"] == "needs_install" and item["kind"] == FILE and item["dest"] == DROPIN_DEST:
                self._install_one_file(item)

    # -- 校验与重启 --------------------------------------------------------
    def daemon_reload(self):
        self.remote.run_checked("sudo systemctl daemon-reload", "daemon-reload")

    def import_smoke(self):
        self.remote.run_checked(
            "cd %s && sudo -u ubuntu /usr/bin/python3 -c \"from content_domains import editorial_contract\""
            % _py(AUTH_SERVICE_ROOT), "auth editorial_contract import")
        self.remote.run_checked(
            "cd %s && sudo -u ubuntu /usr/bin/python3 -c \"from content_domains import editorial_contract\""
            % _py(CONTENT_SERVICE_ROOT), "content editorial_contract import")

    def restart_services(self):
        self.remote.run_checked("sudo systemctl restart huangque-auth", "重启 huangque-auth")
        self.remote.run_checked("systemctl is-active --quiet huangque-auth",
                                "huangque-auth active")
        self.remote.run_checked("sudo systemctl restart huangque-content", "重启 huangque-content")
        self.remote.run_checked("systemctl is-active --quiet huangque-content",
                                "huangque-content active")

    def health_check(self):
        """轮询健康接口：对连接拒绝/启动中/临时非 2xx 做有限重试，超时才失败。"""
        deadline = time.time() + self.health_timeout
        attempt = 0
        while True:
            attempt += 1
            result = self.remote.run(
                "curl -sS -o /dev/null -w '%%{http_code}' --max-time 10 %s" % _py(HEALTH_URL))
            code = (result.stdout or "").strip()
            self.log("健康检查第%d次: code=%s rc=%s"
                     % (attempt, code or "(空)", result.returncode))
            if result.returncode == 0 and code.startswith("2"):
                return
            if time.time() >= deadline:
                raise ReleaseError(
                    "健康检查超时（%d秒内未恢复）: 最后 code=%s"
                    % (self.health_timeout, code or "(空)"))
            time.sleep(self.health_interval)

    def unauth_check(self):
        self.remote.run_checked(
            "code=$(curl -sS -o /dev/null -w '%%{http_code}' --max-time 10 %s); test \"$code\" = 401"
            % _py(UNAUTH_URL), "未登录接口返回 401")

    # -- 回滚 -------------------------------------------------------------
    def rollback(self):
        """依据事务开始时的真实状态回滚：原本存在则恢复，原本不存在则删除。"""
        for dest in reversed(self.installed):
            pre = self.preimage_states.get(dest, {"exists": False})
            if pre.get("exists") and not pre.get("symlink"):
                self._restore_artifact(dest)
                self.log("回滚恢复: " + dest)
            else:
                self._delete_artifact(dest)
                self.log("回滚删除: " + dest)
        self.remote.run("sudo rm -rf %s" % _py(self.backup_dir))
        self.remote.run("sudo systemctl daemon-reload")
        self.remote.run("sudo systemctl restart huangque-auth huangque-content")

    def _delete_artifact(self, dest):
        if dest == RUNTIME_ROOT or dest.startswith(WEBROOT + "/assets/one-click/templates/"):
            self.remote.run("sudo rm -rf %s" % _py(dest))
        else:
            self.remote.run("sudo rm -f %s" % _py(dest))

    def _restore_artifact(self, dest):
        if dest == RUNTIME_ROOT or dest.startswith(WEBROOT + "/assets/one-click/templates/"):
            safe = dest.lstrip("/").replace("/", "_")
            self.remote.run("sudo rm -rf %s && sudo tar -xf %s/%s.tar -C /"
                            % (_py(dest), _py(self.backup_dir), _py(safe)))
        else:
            rel = dest.lstrip("/")
            self.remote.run("sudo cp -a %s/%s %s"
                            % (_py(self.backup_dir), _py(rel), _py(dest)))

    # -- 主流程 -------------------------------------------------------------
    def run(self):
        current = self.current_states()
        self.preimage_states = current
        plan = build_plan(self.repo_root, current)
        drifted = [item for item in plan if item["state"] == "drifted"]
        if drifted:
            raise Drifted("发现漂移，拒绝写入/备份/npm/重启: %s"
                          % ", ".join(item["dest"] for item in drifted))
        if all(item["state"] == "already_postimage" for item in plan):
            return {"result": "already_postimage", "writes": 0, "restarts": 0}
        self.backup(plan)
        try:
            self.install_files(plan)
            self.install_tree(plan)
            self.install_npm(plan)
            self.install_dropin(plan)
            self.daemon_reload()
            self.import_smoke()
            self.restart_services()
            self.health_check()
            self.unauth_check()
        except Exception:
            self.rollback()
            raise
        return {"result": "done", "writes": len(self.installed), "restarts": 2}


def _write_temp_json(payload):
    fd, name = tempfile.mkstemp(prefix="editorial-release-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="口播网感模板专用事务发布器")
    parser.add_argument("--host", default="129.204.166.13")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--check", action="store_true", help="只读预检：校验白名单，不写")
    parser.add_argument("--dry-run", action="store_true", help="计算分类与动作，不写")
    parser.add_argument("--execute", action="store_true", help="事务安装（生产写入）")
    args = parser.parse_args(argv)

    validate_whitelist(ROOT)
    if args.check:
        print("白名单校验通过：%d 个目标" % len(ARTIFACTS))
        for kind, source, dest in ARTIFACTS:
            print("  %-4s %-55s -> %s" % (kind, source, dest))
        return 0
    if args.dry_run:
        plan = build_plan(ROOT, {})
        for item in plan:
            print("%-16s %s" % (item["state"], item["dest"]))
        print("分类：needs_install=%d already_postimage=%d drifted=%d"
              % (sum(1 for i in plan if i["state"] == "needs_install"),
                 sum(1 for i in plan if i["state"] == "already_postimage"),
                 sum(1 for i in plan if i["state"] == "drifted")))
        return 0
    if not args.execute:
        parser.error("必须指定 --check / --dry-run / --execute 之一")

    remote = Remote(args.host, args.user)
    transaction = Transaction(remote)
    result = transaction.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
