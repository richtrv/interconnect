import importlib.util
import json
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pull_premium", ROOT / "scripts" / "pull_premium.py"
)
pull_premium = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pull_premium)


class ResponseValidationTests(unittest.TestCase):
    def test_missing_offers_fails_closed(self):
        with self.assertRaisesRegex(pull_premium.CollectionError, "missing"):
            pull_premium.validate_response({"status": "ok"})

    def test_non_list_offers_fails_closed(self):
        with self.assertRaisesRegex(pull_premium.CollectionError, "list"):
            pull_premium.validate_response({"offers": {}})

    def test_empty_offers_fails_closed(self):
        with self.assertRaisesRegex(pull_premium.CollectionError, "no offers"):
            pull_premium.validate_response({"offers": []})

    def test_query_limit_fails_closed(self):
        offers = [{}] * pull_premium.QUERY_LIMIT
        with self.assertRaisesRegex(pull_premium.CollectionError, "truncated"):
            pull_premium.validate_response({"offers": offers})


class BuildDayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = ROOT / "tests" / "fixtures" / "vast_offers.json"
        cls.offers = json.loads(fixture.read_text())["offers"]

    def test_fixture_is_deduplicated_by_machine(self):
        day = pull_premium.build_day(self.offers, "2026-07-26")

        self.assertTrue(day["valid"])
        self.assertEqual(day["methodology_version"], "v2")
        self.assertEqual(day["standalone_n"], 5)
        self.assertEqual(day["clustered_n"], 5)
        self.assertEqual(day["standalone_median"], 2.4)
        self.assertEqual(day["clustered_median"], 3.4)
        self.assertEqual(day["spread_pct"], 41.67)

        provenance = day["provenance"]
        self.assertEqual(provenance["raw_offer_n"], 14)
        self.assertEqual(provenance["schema_valid_offer_n"], 13)
        self.assertEqual(provenance["usable_offer_n"], 13)
        self.assertEqual(provenance["standalone_offer_n"], 6)
        self.assertEqual(provenance["clustered_offer_n"], 6)
        self.assertEqual(provenance["standalone_machine_n_pretrim"], 5)
        self.assertEqual(provenance["clustered_machine_n_pretrim"], 5)

    def test_missing_or_zero_gpu_fraction_is_rejected(self):
        base = {
            "verification": "verified",
            "gpu_ram": 80000,
            "num_gpus": 8,
            "dph_total": 24.0,
            "bw_nvlink": 900,
        }
        offers = [
            {**base, "machine_id": 1},
            {**base, "machine_id": 2, "gpu_frac": 0},
            {**base, "machine_id": 3, "gpu_frac": 1.0},
        ]
        day = pull_premium.build_day(offers, "2026-07-26")
        self.assertEqual(day["provenance"]["clustered_offer_n"], 1)

    def test_standalone_gpu_fraction_is_not_a_fractional_gpu_filter(self):
        offer = {
            "machine_id": 1,
            "verification": "verified",
            "gpu_ram": 80000,
            "num_gpus": 1,
            "gpu_frac": 0.125,
            "dph_total": 2.5,
            "bw_nvlink": 0,
        }
        day = pull_premium.build_day([offer], "2026-07-26")
        self.assertEqual(day["provenance"]["standalone_offer_n"], 1)
        self.assertEqual(day["standalone_n"], 1)

    def test_non_positive_and_non_finite_prices_are_rejected(self):
        base = {
            "verification": "verified",
            "gpu_ram": 80000,
            "num_gpus": 1,
            "gpu_frac": 1.0,
            "bw_nvlink": 0,
        }
        offers = [
            {**base, "machine_id": 1, "dph_total": 2.5},
            {**base, "machine_id": 2, "dph_total": 0},
            {**base, "machine_id": 3, "dph_total": -1},
            {**base, "machine_id": 4, "dph_total": math.inf},
        ]
        day = pull_premium.build_day(offers, "2026-07-26")
        self.assertEqual(day["provenance"]["schema_valid_offer_n"], 1)
        self.assertEqual(day["standalone_n"], 1)
        self.assertFalse(day["valid"])


if __name__ == "__main__":
    unittest.main()
