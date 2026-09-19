"""Tests for testing utilities."""
import unittest
from strata.testing import (
    create_test_source,
    create_test_model,
    create_test_module,
    create_test_fixtures,
    FreshnessTestHelper,
)


class TestTestSource(unittest.TestCase):
    def test_create_test_source(self):
        """Create a test source."""
        source = create_test_source(
            "events",
            {"id": "int64", "name": "string"},
            ns="test_ns",
            dataset="test_dataset",
        )
        self.assertIn("source events", source)
        self.assertIn("ns: \"test_ns\"", source)
        self.assertIn("dataset: \"test_dataset\"", source)
        self.assertIn("id: int64", source)
        self.assertIn("name: string", source)


class TestTestModel(unittest.TestCase):
    def test_create_test_model_basic(self):
        """Create a basic test model."""
        model = create_test_model("m1", "events")
        self.assertIn("model m1", model)
        self.assertIn("from events", model)

    def test_create_test_model_with_freshness(self):
        """Create a test model with freshness."""
        model = create_test_model("m1", "events", freshness="daily")
        self.assertIn("freshness daily", model)

    def test_create_test_model_with_partition_by(self):
        """Create a test model with partition_by."""
        model = create_test_model("m1", "events", partition_by=["ds", "region"])
        self.assertIn("partition_by [ds, region]", model)

    def test_create_test_model_incremental(self):
        """Create an incremental test model."""
        model = create_test_model(
            "m1",
            "events",
            incremental=True,
            merge_strategy="upsert",
            merge_keys=["id"],
            cdc_column="updated_at",
        )
        self.assertIn("incremental", model)
        self.assertIn("merge_strategy: upsert", model)
        self.assertIn("merge_keys: [id]", model)
        self.assertIn("cdc_column: updated_at", model)

    def test_create_test_model_staleness_ok(self):
        """Create a test model with staleness_ok."""
        model = create_test_model("m1", "events", staleness_ok=True)
        self.assertIn("staleness_ok: \"true\"", model)


class TestTestModule(unittest.TestCase):
    def test_create_test_module(self):
        """Create a complete test module."""
        fixtures = create_test_fixtures()
        file_path = create_test_module(
            fixtures["sources"],
            fixtures["models"],
        )
        self.assertTrue(file_path.endswith(".strata"))
        with open(file_path) as f:
            content = f.read()
        self.assertIn("source events", content)
        self.assertIn("model daily_events", content)


class TestFixtures(unittest.TestCase):
    def test_create_test_fixtures(self):
        """Create test fixtures."""
        fixtures = create_test_fixtures()
        self.assertIn("sources", fixtures)
        self.assertIn("models", fixtures)
        self.assertIn("events", fixtures["sources"])
        self.assertIn("daily_events", fixtures["models"])


class TestFreshnessTestHelper(unittest.TestCase):
    def test_helper_initialization(self):
        """Initialize FreshnessTestHelper."""
        import duckdb
        con = duckdb.connect(":memory:")
        helper = FreshnessTestHelper(con)
        self.assertEqual(helper.con, con)
        self.assertEqual(helper.tables_created, [])


if __name__ == "__main__":
    unittest.main()
