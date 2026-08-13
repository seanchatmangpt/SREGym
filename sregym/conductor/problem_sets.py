"""Named sets of registered SREGym problems.

SREGym-Lite membership is a generated projection of ``ggen/sregym.ttl``.
Edit the ontology and run ``ggen sync run`` instead of editing the generated
module by hand.
"""

from sregym.conductor.generated_problem_sets import SREGYM_LITE_PROBLEMS

PROBLEM_SETS = {"sregym-lite": SREGYM_LITE_PROBLEMS}
