"""Tests for _estimate_local_model_bytes: local model memory guesses before a model is ever loaded."""
import unittest

from server import GB, _estimate_local_model_bytes


class LocalEstimateTests(unittest.TestCase):
    def test_measured_wins(self):
        details = {"parent_model": "gemma4:latest", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"}
        self.assertEqual(_estimate_local_model_bytes("gemma4-64k", details, {"gemma4-64k": 3 * GB, "gemma4:latest": 11 * GB}, 9 * GB), 3 * GB)

    def test_parent_measured(self):
        details = {"parent_model": "gemma4:latest", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"}
        self.assertEqual(_estimate_local_model_bytes("gemma4-64k", details, {"gemma4:latest": 3 * GB}, 9 * GB), int(3 * GB * 1.15))

    def test_params_and_quant(self):
        details = {"parent_model": "", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"}
        got = _estimate_local_model_bytes("m", details, {}, 9 * GB)
        self.assertAlmostEqual(got, 8e9 * 4.8 / 8 + 0.5 * GB, delta=0.01 * GB)

    def test_small_params_f16(self):
        details = {"parameter_size": "137M", "quantization_level": "F16"}
        got = _estimate_local_model_bytes("m", details, {}, 9 * GB)
        self.assertAlmostEqual(got, 137e6 * 16 / 8 + 0.5 * GB, delta=0.01 * GB)

    def test_unknown_quant_uses_8_5(self):
        details = {"parameter_size": "27B", "quantization_level": "Q3_XXL"}
        got = _estimate_local_model_bytes("m", details, {}, 9 * GB)
        self.assertAlmostEqual(got, 27e9 * 8.5 / 8 + 0.5 * GB, delta=0.01 * GB)

    def test_no_details_old_formula(self):
        disk = 9 * GB
        self.assertEqual(_estimate_local_model_bytes("m", None, {}, disk), int(disk * 1.2 + 0.3 * GB))
        self.assertEqual(_estimate_local_model_bytes("m", {}, {}, disk), int(disk * 1.2 + 0.3 * GB))

    def test_bad_params_fall_back(self):
        disk = 4 * GB
        self.assertEqual(_estimate_local_model_bytes("m", {"parameter_size": "lots", "quantization_level": "Q4_0"}, {}, disk),
                         int(disk * 1.2 + 0.3 * GB))


if __name__ == "__main__":
    unittest.main()
