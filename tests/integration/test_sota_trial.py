"""Real SOTA-agent trial integration test for SREGym.

Unlike ``smoke_test.py`` (which submits a hardcoded placeholder string and
expects mitigation to fail), this test drives a real ``dspy.ReAct`` loop
against a real, already-running cluster through the exact in-process
``Conductor`` API that ``smoke_test.py`` exercises: ``Conductor(config=...)``,
``conductor.problem_id = ...``, ``await conductor.start_problem()``, polling
``conductor.submission_stage``, and ``await conductor.submit(solution)``.

The agent is given exactly one real tool bound to ``conductor.kubectl.exec_command``
(the same ``KubeCtl`` the harness itself uses -- no MCP server, no subprocess
sidecar). The tool enforces a stage-dependent command allowlist in Python,
*before* any real command reaches the cluster:

* diagnosis stage: read-only kubectl verbs only (get/describe/logs/top/events).
* mitigation stage: the above plus bounded mutation verbs (patch/rollout/scale)
  and single-pod deletion (``delete pod ...``), never namespace/pvc/pv/secret
  deletion or anything else that can destroy data.

Any LM-proposed command outside the active stage's allowlist is refused (not
executed) and the tool returns a refusal string to the LM so it can retry.

Skipped when ``GROQ_API_KEY`` is unset, mirroring this repo's own real,
no-mock live-model gating convention (see
``tests/llm_as_a_judge/test_stratus_rejudge.py``'s ``skipif`` on
``JUDGE_MODEL_ID``): a real model server is required, never a mocked one.

Because this exercises real, non-deterministic model behavior against a real
fault, the test makes NO assertion about diagnosis/mitigation success -- a
real SOTA trial's outcome is data, not a pass/fail gate. It asserts only that
the real pipeline reached completion: ``submission_stage == "done"`` and both
``"Diagnosis"`` and ``"Mitigation"`` keys are present in ``conductor.results``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path

import pytest

from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.constants import StartProblemResult

logger = logging.getLogger("all.sregym.tests.sota_trial")

PROBLEM_ID = os.environ.get("PROBLEM_ID", "misconfig_app_hotel_res")
RESULTS_PATH = Path(os.environ.get("SOTA_TRIAL_RESULTS_PATH", "sota_trial_results.json"))

POLL_TIMEOUT_S = 600  # 10 minutes per stage
POLL_INTERVAL_S = 5
MAX_REACT_ITERS = 15

pytestmark = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"),
    reason="GROQ_API_KEY is not set; live dspy.LM is unavailable",
)

# ---------------------------------------------------------------------------
# kubectl command allowlists
# ---------------------------------------------------------------------------

# Verbs permitted at any stage: pure reads, no cluster state changes.
_READ_ONLY_VERBS = {"get", "describe", "logs", "top", "events", "explain", "api-resources", "version"}

# Additional verbs permitted only during mitigation: bounded mutation.
_MITIGATION_MUTATION_VERBS = {"patch", "rollout", "scale"}

# Sub-forms that are never allowed regardless of stage, even if the leading
# verb would otherwise be permitted (e.g. "delete namespace", "delete pvc").
_FORBIDDEN_SUBSTRINGS = (
    "delete namespace",
    "delete ns ",
    "delete pvc",
    "delete pv ",
    "delete persistentvolume",
    "delete secret",
    "delete configmap",
    "delete crd",
    "delete customresourcedefinition",
    "delete all",
    "--all-namespaces",
    " -A",
    "delete node",
)


def _leading_kubectl_verb(command: str) -> tuple[str, list[str]] | None:
    """Parse a shell command string as ``kubectl <verb> ...``.

    Returns (verb, tokens) or None if the command does not start with a bare
    ``kubectl`` invocation (e.g. contains a pipe, subshell, or other binary).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "kubectl":
        return None
    if len(tokens) < 2:
        return None
    # Reject any shell metacharacters that could smuggle a second command.
    if any(ch in command for ch in ("|", ";", "&", "$(", "`", ">", "<")):
        return None
    return tokens[1], tokens


def _is_allowed(command: str, stage: str) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    lowered = command.lower()
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        if forbidden in lowered:
            return False, f"command contains forbidden pattern {forbidden!r}"

    parsed = _leading_kubectl_verb(command)
    if parsed is None:
        return False, "only single, bare 'kubectl <verb> ...' commands are permitted (no pipes/shell operators)"
    verb, tokens = parsed

    if verb == "delete":
        # Only a single-pod delete is ever permitted, and only during mitigation.
        if stage != "mitigation":
            return False, "delete is only permitted during the mitigation stage"
        if "pod" not in tokens:
            return False, "delete is only permitted for a single pod (kubectl delete pod <name> ...)"
        return True, "ok"

    if verb in _READ_ONLY_VERBS:
        return True, "ok"

    if verb in _MITIGATION_MUTATION_VERBS:
        if stage == "mitigation":
            return True, "ok"
        return False, f"'{verb}' is only permitted during the mitigation stage (current stage: {stage!r})"

    return False, f"kubectl verb '{verb}' is not on the allowlist for stage {stage!r}"


# ---------------------------------------------------------------------------
# The trial
# ---------------------------------------------------------------------------


def _make_kubectl_tool(conductor: Conductor, stage_getter):
    """Build a dspy Tool-compatible function bound to conductor.kubectl.

    ``stage_getter`` is called at invocation time (not bind time) so the same
    tool instance can be reused across the diagnosis and mitigation stages
    while always enforcing the *current* stage's allowlist.
    """

    def run_kubectl(command: str) -> str:
        """Run a real kubectl command against the live cluster and return its output.

        Only ``kubectl <verb> ...`` commands are accepted (no shell pipes or
        chaining). During diagnosis only read-only verbs (get, describe, logs,
        top, events, explain) are permitted. During mitigation, patch,
        rollout, scale, and single-pod delete are additionally permitted.
        Anything else -- deleting a namespace, PVC, PV, secret, or any
        wildcard/-A delete -- is always refused.
        """
        stage = stage_getter()
        allowed, reason = _is_allowed(command, stage)
        if not allowed:
            logger.warning("Refusing kubectl command at stage %r: %s -- %r", stage, reason, command)
            return f"REFUSED: {reason}. Command was not executed: {command!r}"
        logger.info("Executing allowed kubectl command at stage %r: %s", stage, command)
        return conductor.kubectl.exec_command(command)

    return run_kubectl


def _run_react_stage(kubectl_tool, stage_name: str, task_description: str) -> str:
    """Run a real dspy.ReAct loop for one stage and return its free-text answer."""
    import dspy

    lm = dspy.LM(
        "groq/openai/gpt-oss-20b",
        api_key=os.environ["GROQ_API_KEY"],
        temperature=0.0,
        max_tokens=4000,
    )

    class StageSignature(dspy.Signature):
        """Investigate a live Kubernetes cluster using the run_kubectl tool and
        report findings as free text. Use only the provided tool to inspect or
        (during mitigation) fix the cluster -- never guess without checking
        real cluster state first."""

        task: str = dspy.InputField()
        report: str = dspy.OutputField(desc="Free-text summary of findings/actions taken.")

    agent = dspy.ReAct(StageSignature, tools=[kubectl_tool], max_iters=MAX_REACT_ITERS)

    with dspy.context(lm=lm):
        prediction = agent(task=task_description)

    return getattr(prediction, "report", str(prediction))


async def _poll_until(conductor: Conductor, target_stage_not: str, timeout_s: int, interval_s: int) -> None:
    elapsed = 0
    while conductor.submission_stage == target_stage_not:
        if elapsed >= timeout_s:
            pytest.fail(
                f"Timed out after {timeout_s}s waiting for stage to advance past "
                f"{target_stage_not!r}. Stage: {conductor.submission_stage}"
            )
        await asyncio.sleep(interval_s)
        elapsed += interval_s


async def _run_sota_trial() -> dict:
    timing: dict[str, float] = {}
    wall_start = time.time()

    conductor = Conductor(config=ConductorConfig(deploy_loki=False))
    conductor.problem_id = PROBLEM_ID

    t0 = time.time()
    result = await conductor.start_problem()
    timing["start_problem_s"] = time.time() - t0
    assert result == StartProblemResult.SUCCESS, f"start_problem returned {result}"

    # current_stage() is a moving target read live by the tool closure.
    current_stage = {"value": conductor.submission_stage}

    def get_current_stage() -> str:
        return current_stage["value"]

    kubectl_tool = _make_kubectl_tool(conductor, get_current_stage)

    # ---- Diagnosis stage (if the problem has one) -------------------------
    if conductor.submission_stage == "diagnosis":
        current_stage["value"] = "diagnosis"
        t0 = time.time()
        diagnosis_report = _run_react_stage(
            kubectl_tool,
            "diagnosis",
            f"A fault has been injected into problem {PROBLEM_ID!r}. Using only "
            "read-only kubectl commands (get/describe/logs/top/events), "
            "investigate the cluster and report what is wrong and why.",
        )
        timing["diagnosis_react_s"] = time.time() - t0

        t0 = time.time()
        submit_response = await conductor.submit(diagnosis_report)
        assert submit_response.get("status") == "ok", f"diagnosis submit response: {submit_response}"
        await _poll_until(conductor, "diagnosis", POLL_TIMEOUT_S, POLL_INTERVAL_S)
        timing["diagnosis_eval_s"] = time.time() - t0

    # ---- Mitigation stage (if the problem has one) -------------------------
    if conductor.submission_stage == "mitigation":
        current_stage["value"] = "mitigation"
        t0 = time.time()
        mitigation_report = _run_react_stage(
            kubectl_tool,
            "mitigation",
            f"A fault has been injected into problem {PROBLEM_ID!r}. Using kubectl "
            "(reads, plus patch/rollout/scale/single-pod-delete as needed), "
            "diagnose and repair the cluster so the affected service recovers. "
            "Report what you found and what you changed.",
        )
        timing["mitigation_react_s"] = time.time() - t0

        t0 = time.time()
        submit_response = await conductor.submit(mitigation_report)
        assert submit_response.get("status") == "ok", f"mitigation submit response: {submit_response}"
        await _poll_until(conductor, "mitigation", POLL_TIMEOUT_S, POLL_INTERVAL_S)
        timing["mitigation_eval_s"] = time.time() - t0

    # ---- Wait for full teardown to reach "done" ----------------------------
    elapsed = 0
    while conductor.submission_stage != "done":
        if elapsed >= POLL_TIMEOUT_S:
            pytest.fail(
                f"Timed out after {POLL_TIMEOUT_S}s waiting for pipeline to reach "
                f"'done'. Stage: {conductor.submission_stage}"
            )
        await asyncio.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

    timing["wall_clock_total_s"] = time.time() - wall_start

    return {
        "problem_id": PROBLEM_ID,
        "submission_stage": conductor.submission_stage,
        "results": conductor.results,
        "timing": timing,
    }


@pytest.mark.integration
def test_sota_trial():
    """Real dspy.ReAct SOTA trial: diagnosis + mitigation against a live cluster.

    Asserts only that the real pipeline completed -- it makes no assertion
    about whether the agent's diagnosis/mitigation actually succeeded, since
    that outcome is data about the model, not a property this test enforces.
    """
    payload = asyncio.run(_run_sota_trial())

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Wrote SOTA trial results to %s", RESULTS_PATH)

    assert payload["submission_stage"] == "done", (
        f"Expected pipeline to reach 'done', got {payload['submission_stage']!r}"
    )
    assert "Diagnosis" in payload["results"], f"Missing 'Diagnosis' key in results: {payload['results']}"
    assert "Mitigation" in payload["results"], f"Missing 'Mitigation' key in results: {payload['results']}"
