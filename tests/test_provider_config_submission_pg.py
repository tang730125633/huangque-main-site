"""Same real admission/queue/worker tests with authoritative PostgreSQL config."""
import os
from unittest.mock import patch
from tests import pg_harness
from tests import test_provider_config_submission as admission


class PostgresProviderSubmissionTests(admission.ProviderSubmissionTests):
    config_store = 'postgres'

    @classmethod
    def setUpClass(cls):
        cls.pg_url = pg_harness.postgres_url()

    def setUp(self):
        env=patch.dict(os.environ, {'HQ_DATABASE_URL':self.pg_url})
        env.start();self.addCleanup(env.stop)
        import psycopg
        # Dedicated fixture target in the isolated test database, never production.
        with psycopg.connect(self.pg_url) as c:
            c.execute('DELETE FROM ops.provider_config_runtime WHERE target_id=%s',('image.seedream',))
            c.execute('DELETE FROM ops.provider_config_versions WHERE target_id=%s',('image.seedream',))
        super().setUp()
