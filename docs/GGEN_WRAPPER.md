# ggen wrapper for SREGym

This fork keeps SREGym as the execution substrate and makes ggen the deterministic manufacturing layer for benchmark composition.

```text
RDF ontology (O*)
  -> ggen validation gate
  -> SPARQL/Tera projection (mu)
  -> generated_problem_sets.py
  -> existing PROBLEM_SETS
  -> existing main.py / Conductor / ProblemRegistry
  -> existing fault injection + oracles
```

## Authority boundary

`ggen/sregym.ttl` is the editing authority for the migrated SREGym-Lite suite. `sregym/conductor/generated_problem_sets.py` is a generated projection and must not be hand-edited. The existing `Problem` implementations, `ProblemRegistry`, fault injectors, oracles, and agent isolation remain unchanged.

The ontology records each benchmark problem's stable runtime ID plus application, fault layer, and fault family. Those dimensions are deliberately graph data rather than Python branching so future suites can be manufactured by SPARQL selection without adding benchmark-selection code.

## Regenerate

With `seanchatmangpt/ggen` checked out adjacent to this repository at commit `41cd378c6f55de6ed3991fdba60a7c25b68546b9`:

```bash
cargo +nightly-2026-06-22 run \
  --manifest-path ../ggen/Cargo.toml \
  -p ggen-cli-lib --bin ggen -- sync run
python scripts/verify_ggen_projection.py
python -m unittest -q tests/test_ggen_projection.py
```

A successful `ggen sync run` also emits ggen's generation receipt. CI repeats the same generation from the pinned ggen commit and refuses if the checked-in projection drifts.

## Extension rule

To add or reclassify a migrated benchmark problem, edit `ggen/sregym.ttl`, not the generated Python. The admission gate requires every problem to have an identifier, executable runtime problem ID, deterministic order, application, fault layer, fault family, and suite membership before projection.

This first slice migrates the recommended 21-problem SREGym-Lite composition because it is a complete executable benchmark surface already exposed by SREGym's `--suite sregym-lite` CLI. The remaining legacy registry entries remain fenced and executable; they can be migrated into the graph incrementally without changing their runtime implementations.
