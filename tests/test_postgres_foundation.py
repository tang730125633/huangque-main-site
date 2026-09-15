import os
import unittest

from server.db import postgres


class PostgresFoundationTest(unittest.TestCase):
    def tearDown(self):
        postgres.close_pool()

    def test_pool_settings_are_validated_without_connecting(self):
        old = {key: os.environ.get(key) for key in ("HQ_DB_POOL_MIN", "HQ_DB_POOL_MAX")}
        try:
            os.environ["HQ_DB_POOL_MIN"] = "11"
            os.environ["HQ_DB_POOL_MAX"] = "10"
            with self.assertRaisesRegex(RuntimeError, "cannot exceed"):
                postgres._settings()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_alembic_uses_psycopg3_dialect(self):
        old = os.environ.get("HQ_DATABASE_URL")
        try:
            os.environ["HQ_DATABASE_URL"] = "postgresql://user:pass@example/db"
            self.assertEqual(
                postgres.sqlalchemy_url(),
                "postgresql+psycopg://user:pass@example/db",
            )
        finally:
            if old is None:
                os.environ.pop("HQ_DATABASE_URL", None)
            else:
                os.environ["HQ_DATABASE_URL"] = old

    @unittest.skipUnless(os.environ.get("HQ_DATABASE_URL"), "PostgreSQL integration URL not configured")
    def test_migrated_database_and_pool(self):
        self.assertTrue(postgres.healthcheck())
        with postgres.connection() as conn:
            schemas = {
                row["schema_name"]
                for row in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = ANY(%s)",
                    (["identity", "ledger", "jobs", "media", "workflow",
                      "agent", "render", "routing", "crm", "ops"],),
                )
            }
            self.assertEqual(len(schemas), 10)
            tables = {
                row["table_name"]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'ops'"
                )
            }
            self.assertTrue({"data_migration_runs", "data_migration_items"} <= tables)
            agent_tables = {
                row["table_name"]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'agent'"
                )
            }
            self.assertIn("sessions", agent_tables)


if __name__ == "__main__":
    unittest.main()
