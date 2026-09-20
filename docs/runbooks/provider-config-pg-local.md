本地隔离 PostgreSQL（不跳过 PG 用例）：

    pip install --target "E:\AI\pg-tools\embedded-pg" embedded-postgres "psycopg[binary]" psycopg-pool alembic sqlalchemy
    python tests/pg_harness.py --fresh        # 起库 + alembic upgrade head，打印 HQ_DATABASE_URL

注意：Windows 上 PG 二进制与数据目录**必须 ASCII 路径**（非 ASCII 会在 initdb bootstrap 阶段
报 invalid byte sequence for encoding "UTF8"），且需 --locale=C。
