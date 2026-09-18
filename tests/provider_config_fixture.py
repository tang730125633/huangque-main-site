"""Provider config fixtures: dedicated test PostgreSQL only, never production."""
import os
from tests import pg_harness

_url = None


def configure(testcase, pc):
    global _url
    if _url is None:
        _url = pg_harness.postgres_url()
    os.environ['HQ_DATABASE_URL'] = _url
    os.environ['HQ_ADMIN_CONFIG_STORE'] = 'postgres'
    pc.admin_config_store.close_pool()
    testcase.addCleanup(pc.admin_config_store.close_pool)
    import psycopg
    with psycopg.connect(_url) as conn:
        conn.execute('DELETE FROM ops.provider_config_runtime')
        conn.execute('DELETE FROM ops.provider_config_versions')
    pc.invalidate()
    return _url
