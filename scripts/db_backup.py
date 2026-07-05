#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄雀 DB 每日备份 → 腾讯云 COS（异地对象存储）。

- sqlite3 在线 `.backup`（热备、不锁表、保证一致性），gzip 压缩，分块上传 COS。
- 对象放 `db-backups/<UTC日期>/<name>.gz`，保留 RETAIN_DAYS 天、自动清理更早的。
- 任一库失败 → 复用漂移哨兵的飞书通道告警，退出码非 0（cron 可感知）。
- 密钥只从服务器 content.env 读，绝不写进 git。

用法：python3 db_backup.py            # 正常备份
      python3 db_backup.py --selftest  # 只测 COS 连通与飞书告警，不落备份
部署：cron 每日跑（见 issue #188）。
"""
import os, sys, sqlite3, subprocess, time, glob

HOME = "/home/ubuntu"
CONTENT_ENV = os.path.join(HOME, "content-api", "content.env")
RETAIN_DAYS = 14

# (库路径, COS 里的备份名)——账号/积分最关键，jobs/获客/配置一并保
DBS = [
    (os.path.join(HOME, "auth-service", "users.db"),            "users.db"),
    (os.path.join(HOME, "content-api", "content_jobs.db"),      "content_jobs.db"),
    (os.path.join(HOME, "content-api", "audio_assets.db"),      "audio_assets.db"),
    (os.path.join(HOME, "content-api", "admin_config.db"),      "admin_config.db"),
    (os.path.join(HOME, "content-api", "feature_flags.db"),     "feature_flags.db"),
    (os.path.join(HOME, "leadgen-server", "jobs.db"),           "leadgen_jobs.db"),
]


def _load_env():
    env = {}
    with open(CONTENT_ENV) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _cos_client(env):
    from qcloud_cos import CosConfig, CosS3Client
    cfg = CosConfig(Region=env.get("COS_REGION", "ap-guangzhou"),
                    SecretId=env["COS_SECRET_ID"], SecretKey=env["COS_SECRET_KEY"])
    return CosS3Client(cfg), env["COS_BUCKET"]


def _alert(text):
    try:
        sys.path.insert(0, os.path.join(HOME, "hq-drift"))
        from drift_sentinel import _feishu_send
        return _feishu_send(text)
    except Exception as e:
        print("飞书告警发送失败:", e)
        return False


def _backup_one(src, name, tmpdir):
    """在线备份 src → gz 文件，返回本地 gz 路径。"""
    raw = os.path.join(tmpdir, "hqbak_" + name)
    gz = raw + ".gz"
    for p in (raw, gz):
        try: os.unlink(p)
        except OSError: pass
    con = sqlite3.connect(src)
    try:
        bck = sqlite3.connect(raw)
        try:
            con.backup(bck)          # 在线一致性快照
        finally:
            bck.close()
    finally:
        con.close()
    # 流式 gzip（大库 574M 也不吃内存）
    subprocess.run(["gzip", "-f", raw], check=True, timeout=600)
    return gz


def _cleanup_old(cli, bucket):
    cutoff = time.time() - RETAIN_DAYS * 86400
    marker = ""
    while True:
        r = cli.list_objects(Bucket=bucket, Prefix="db-backups/", Marker=marker, MaxKeys=1000)
        for obj in r.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) >= 2:
                try:
                    d = time.mktime(time.strptime(parts[1], "%Y-%m-%d"))
                except ValueError:
                    continue
                if d < cutoff:
                    cli.delete_object(Bucket=bucket, Key=obj["Key"])
                    print("清理旧备份", obj["Key"])
        if r.get("IsTruncated") == "true" or r.get("IsTruncated") is True:
            marker = r.get("NextMarker", "")
        else:
            break


def main():
    selftest = "--selftest" in sys.argv
    env = _load_env()
    cli, bucket = _cos_client(env)
    date = time.strftime("%Y-%m-%d", time.gmtime())
    if selftest:
        cli.list_objects(Bucket=bucket, Prefix="db-backups/", MaxKeys=1)
        print("COS 连通 OK · bucket", bucket)
        print("飞书自检:", _alert("【DB备份自检】黄雀数据库备份通道正常，可忽略 🙏"))
        return
    tmpdir = "/tmp"
    failed = []
    done = []
    for src, name in DBS:
        if not os.path.exists(src):
            print("跳过(不存在):", src); continue
        try:
            gz = _backup_one(src, name, tmpdir)
            key = "db-backups/%s/%s.gz" % (date, name)
            cli.upload_file(Bucket=bucket, Key=key, LocalFilePath=gz, EnableMD5=False)
            size = os.path.getsize(gz)
            os.unlink(gz)
            print("OK", key, size)
            done.append("%s(%dKB)" % (name, size // 1024))
        except Exception as e:
            failed.append("%s: %s" % (name, str(e)[:120]))
            print("FAIL", name, e)
    try:
        _cleanup_old(cli, bucket)
    except Exception as e:
        print("清理旧备份失败(非致命):", e)
    if failed:
        _alert("【DB备份失败】黄雀 %s：\n%s\n(成功: %s)" % (date, "\n".join(failed), ", ".join(done) or "无"))
        sys.exit(1)
    print("全部备份完成", date, "|", ", ".join(done))


if __name__ == "__main__":
    main()
