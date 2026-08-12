#!/usr/bin/env python3
"""Dependency-free verifier for the SREGym ggen projection boundary."""

from __future__ import annotations

import re
import runpy
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY = ROOT / "ggen" / "sregym.ttl"
GENERATED = ROOT / "sregym" / "conductor" / "generated_problem_sets.py"
MANIFEST = ROOT / "ggen.toml"
PROBLEM_SETS = ROOT / "sregym" / "conductor" / "problem_sets.py"
REGISTRY = ROOT / "sregym" / "conductor" / "problems" / "registry.py"

BLOCK_RE = re.compile(
    r"sre:[^\s]+\s+a\s+sre:BenchmarkProblem\s*;(?P<body>.*?)\s*\.\s*$",
    re.MULTILINE | re.DOTALL,
)
FIELD_RE = {
    "ontology_id": re.compile(r'dcterms:identifier\s+"([^"]+)"'),
    "runtime_id": re.compile(r'sre:runtimeProblemId\s+"([^"]+)"'),
    "order": re.compile(r"sre:order\s+(\d+)"),
    "application": re.compile(r'sre:application\s+"([^"]+)"'),
    "fault_layer": re.compile(r'sre:faultLayer\s+"([^"]+)"'),
    "fault_family": re.compile(r'sre:faultFamily\s+"([^"]+)"'),
}


def fail(message: str) -> None:
    raise SystemExit(f"REFUSED_GGEN_PROJECTION: {message}")


def ontology_rows() -> list[dict[str, str | int]]:
    text = ONTOLOGY.read_text()
    rows: list[dict[str, str | int]] = []
    for match in BLOCK_RE.finditer(text):
        body = match.group("body")
        row: dict[str, str | int] = {}
        for name, pattern in FIELD_RE.items():
            field = pattern.search(body)
            if field is None:
                fail(f"ontology problem is missing {name}")
            row[name] = int(field.group(1)) if name == "order" else field.group(1)
        if "dcterms:isPartOf sre:sregym-lite" not in body:
            fail(f"{row['runtime_id']} is not admitted to sregym-lite")
        rows.append(row)
    if not rows:
        fail("ontology contains no benchmark problems")
    rows.sort(key=lambda row: int(row["order"]))
    orders = [row["order"] for row in rows]
    if orders != list(range(1, len(rows) + 1)):
        fail(f"orders are not contiguous: {orders}")
    return rows


def main() -> None:
    manifest = tomllib.loads(MANIFEST.read_text())
    rules = manifest.get("generation", {}).get("rules", [])
    if not any(
        rule.get("name") == "sregym-problem-sets"
        and rule.get("output_file") == "sregym/conductor/generated_problem_sets.py"
        for rule in rules
    ):
        fail("ggen.toml does not bind sregym-problem-sets to the generated module")

    rows = ontology_rows()
    generated = runpy.run_path(str(GENERATED))
    actual_ids = tuple(generated["SREGYM_LITE_PROBLEMS"])
    expected_ids = tuple(str(row["runtime_id"]) for row in rows)
    if actual_ids != expected_ids:
        fail(f"generated suite drift: expected {expected_ids!r}, got {actual_ids!r}")

    metadata = generated["PROBLEM_METADATA"]
    if set(metadata) != set(expected_ids):
        fail("generated metadata keys do not match admitted problem ids")
    for row in rows:
        runtime_id = str(row["runtime_id"])
        expected = {
            "ontology_id": row["ontology_id"],
            "application": row["application"],
            "fault_layer": row["fault_layer"],
            "fault_family": row["fault_family"],
        }
        if metadata[runtime_id] != expected:
            fail(f"metadata drift for {runtime_id}: {metadata[runtime_id]!r} != {expected!r}")

    registry_checked = False
    if REGISTRY.exists():
        registry_text = REGISTRY.read_text()
        missing = [runtime_id for runtime_id in expected_ids if f'"{runtime_id}"' not in registry_text]
        if missing:
            fail(f"ontology projects runtime problem ids absent from ProblemRegistry: {missing}")
        registry_checked = True

    problem_sets_text = PROBLEM_SETS.read_text()
    if "from sregym.conductor.generated_problem_sets import SREGYM_LITE_PROBLEMS" not in problem_sets_text:
        fail("runtime problem_sets.py is not wired to the generated projection")

    registry_note = " + native registry closure" if registry_checked else " (registry source not materialized)"
    print(f"PARTIAL_ALIVE ggen projection structure: {len(rows)} admitted SREGym-Lite problems{registry_note}")


if __name__ == "__main__":
    main()
