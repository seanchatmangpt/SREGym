# SREGym migration plan v26.11.16

Standing: UNVERIFIED

## Identity
- Path: /Users/sac/SREGym
- Remote: https://github.com/seanchatmangpt/SREGym.git (fork; main is 53 behind origin/main)
- Branch: sota-trial-runner@2887b31 (no upstream tracking)
- Last commit: 2026-08-12; commits: 4 (shallow-ish fork history); size: 296 MB (.git 154 MB)
- Languages/build: Python (pyproject.toml, uv.lock), docker/, kind/
- Nested repos: SREGym-applications (submodule, heads/main @2b2f9c6, 131 MB, shows modified)

## Observed state
- Dirty 1: ` M SREGym-applications` (submodule pointer drift)
- Untracked 2: `docs/jira/` (v26.9.19 + this catalogue)
- Ahead/behind: branch sota-trial-runner has NO upstream; unpushed branch count 1
- Stash: 0. Build dirs: none. Submodule: SREGym-applications (initialized)

## What it is
SREGym is an AI-native benchmarking platform for SRE agents (live Kubernetes environments
with injected problems). Sean's fork adds a free-CI "SOTA trial runner" and Groq retry fixes.

## Classification
WARM (census agrees; last work Aug 2026, three local commits on top of upstream).

## Migration risk
MEDIUM. Branch sota-trial-runner (3 commits: fb1a42c, 081b5cb, 2887b31) has no upstream and
may be unpushed; docs/jira untracked; submodule pointer drift. No .env/.db found at top level.

## Recommended action
COMMIT-PUSH
1. `cd /Users/sac/SREGym && git log origin/sota-trial-runner..sota-trial-runner --oneline`
   (if origin branch missing, it is entirely unpushed)
2. `git add docs/jira && git commit -m "docs: jira catalogue v26.11.16"`
3. DECISION: keep submodule pointer drift? If yes `git add SREGym-applications` and commit.
4. `git push -u origin sota-trial-runner`
5. Then CLONE-FRESH on new machine with `--recurse-submodules`.

## First commands on the new machine
`git clone --recurse-submodules https://github.com/seanchatmangpt/SREGym.git && uv sync`

## See Also
- ../v26.9.19/
- /Users/sac/docs/jira/v26.11.16/README.md
