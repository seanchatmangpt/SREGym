import runpy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GGenProjectionTests(unittest.TestCase):
    def test_generated_sregym_lite_has_21_unique_runtime_problem_ids(self):
        generated = runpy.run_path(str(ROOT / "sregym" / "conductor" / "generated_problem_sets.py"))
        problems = generated["SREGYM_LITE_PROBLEMS"]
        self.assertEqual(len(problems), 21)
        self.assertEqual(len(set(problems)), 21)
        self.assertEqual(set(problems), set(generated["PROBLEM_METADATA"]))

    def test_problem_sets_runtime_uses_generated_projection(self):
        text = (ROOT / "sregym" / "conductor" / "problem_sets.py").read_text()
        self.assertIn("generated_problem_sets import SREGYM_LITE_PROBLEMS", text)
        self.assertIn('PROBLEM_SETS = {"sregym-lite": SREGYM_LITE_PROBLEMS}', text)


if __name__ == "__main__":
    unittest.main()
