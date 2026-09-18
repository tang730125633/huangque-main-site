# -*- coding: utf-8 -*-
"""启动一个隔离的本地 PostgreSQL（embedded-postgres，数据目录在 E:\\AI 下）。

用法:
    python _pg_local.py            # 启动并打印链接；进程退出时按 cleanup_mode 停库
    python _pg_local.py --stop     # 仅停库

环境变量:
    PG_LOCAL_DATA   数据目录（默认 E:\\AI\\缓存\\pgdata-provider-config）
    PG_LOCAL_DB     需要创建的数据库名（默认 huangque_staging）
"""
import os
import sys
from pathlib import Path

DEFAULT_DATA = r"E:\AI\缓存\pgdata-provider-config"
DEFAULT_DB = "huangque_staging"


def main() -> int:
    data = Path(os.environ.get("PG_LOCAL_DATA") or DEFAULT_DATA)
    dbname = os.environ.get("PG_LOCAL_DB") or DEFAULT_DB
    data.mkdir(parents=True, exist_ok=True)

    import embedded_postgres as ep

    server = ep.get_server(str(data), cleanup_mode=None)
    server.ensure_pgdata_inited()
    server.ensure_postgres_running()
    base = server.get_uri()          # 形如 postgresql://postgres:@127.0.0.1:PORT/postgres
    if "--stop" in sys.argv:
        server.cleanup()
        print("stopped")
        return 0

    print("BASE_URI=%s" % base)
    # 建一个干净的库（已存在则重建）
    try:
        server.psql("postgres", "DROP DATABASE IF EXISTS %s" % dbname)
    except Exception as exc:  # noqa: BLE001
        print("drop skipped: %s" % exc)
    server.psql("postgres", "CREATE DATABASE %s" % dbname)

    uri = base.replace("/postgres", "/%s" % dbname)
    if "?" in uri:
        head, _, tail = uri.partition("?")
        uri = head.replace("/postgres", "/%s" % dbname) + "?" + tail
    print("HQ_DATABASE_URL=%s" % uri)
    print("PG_LOCAL_DATA=%s" % data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
