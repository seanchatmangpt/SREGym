#!/usr/bin/env bash
# postCreate.sh
#
# Runs once, inside the freshly-started devcontainer (real dockerd via the
# docker-in-docker feature — no Colima anywhere on this path, so failure
# modes (1) and (2) from the design brief cannot occur here by construction:
# there is no host docker-context to get silently reset, and every run
# rebuilds the cluster from this repo's own kind/setup_kind_cluster.sh
# against a known-good config, never a leftover/wrongly-shaped one).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

echo "======================================================================"
echo " SREGym devcontainer postCreate"
echo "======================================================================"

# ── Arch autodetect, single source of truth reused by every later step ────
UNAME_M="$(uname -m)"
case "${UNAME_M}" in
    x86_64|amd64)   SREGYM_ARCH="x86" ;;
    aarch64|arm64)  SREGYM_ARCH="arm" ;;
    *)
        echo "❌ Unsupported architecture: ${UNAME_M}" >&2
        exit 1
        ;;
esac
echo "==> Detected architecture: ${UNAME_M} -> SREGYM_ARCH=${SREGYM_ARCH}"

KIND_CLUSTER_NAME="kind"

# ── Generic retry-with-backoff wrapper ─────────────────────────────────────
# Used everywhere in this script that touches the network for something that
# could NOT be fully baked into the image (a fresh kind cluster's own
# metrics-server rollout confirming Available=True, and any residual pull
# that fell through prepull-images.sh's bake step). Exponential backoff,
# capped attempts, every attempt logged so a stuck postCreate is diagnosable
# from the Codespaces creation log alone.
retry_with_backoff() {
    local max_attempts="$1"; shift
    local base_delay="$1"; shift
    local description="$1"; shift
    local attempt=1
    local delay="${base_delay}"

    until "$@"; do
        if (( attempt >= max_attempts )); then
            echo "❌ ${description}: failed after ${attempt} attempts. Giving up." >&2
            return 1
        fi
        echo "⚠️  ${description}: attempt ${attempt}/${max_attempts} failed. Retrying in ${delay}s..." >&2
        sleep "${delay}"
        attempt=$(( attempt + 1 ))
        delay=$(( delay * 2 ))
    done
    echo "✅ ${description}: succeeded on attempt ${attempt}."
}

# ── Step 1: Python environment (uv sync, per this repo's own README/CLAUDE.md) ─
echo "==> Step 1: uv sync"
retry_with_backoff 5 5 "uv sync" uv sync

# ── Step 2: Delete any stale cluster from a previous rebuild ──────────────
# This is the direct structural fix for failure mode (2) (a wrongly-shaped
# stale single-node/no-CNI cluster): postCreate ALWAYS deletes and rebuilds,
# it never reuses whatever a previous container run happened to leave behind.
if kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER_NAME}"; then
    echo "==> Step 2: Deleting pre-existing kind cluster '${KIND_CLUSTER_NAME}' (never reused, always rebuilt fresh)"
    kind delete cluster --name "${KIND_CLUSTER_NAME}"
else
    echo "==> Step 2: No pre-existing kind cluster found."
fi

# ── Step 3: Load baked images into this container's own dockerd ──────────
# Loads the images .devcontainer/Dockerfile baked at build time via
# skopeo/prepull-images.sh's "bake" mode. This must happen with `docker
# load` BEFORE the cluster exists (populates this daemon's local image
# cache) and the kind-node push happens again per-node once the cluster is
# up in Step 5, since kind load needs a live cluster to target.
echo "==> Step 3: docker load baked images into the devcontainer daemon"
if [[ -f /opt/prepulled-images/manifest.txt ]]; then
    while IFS=$'\t' read -r image fname; do
        [[ -z "${image}" ]] && continue
        echo "    docker load: ${image}"
        docker load -i "/opt/prepulled-images/${fname}" || \
            echo "⚠️  docker load failed for ${image}; will fall back to a live pull in Step 5." >&2
    done < /opt/prepulled-images/manifest.txt
else
    echo "⚠️  No bake manifest found at /opt/prepulled-images/manifest.txt (build-time bake did not run or failed entirely)." >&2
fi

# ── Step 4: Bring up the real kind cluster from THIS repo's own script ────
# This is the direct structural fix for failure mode (1): there is no Colima
# in this VM at all, so there is no docker-context to silently reset and no
# stale ~/.kube/config host/port mismatch to resolve against. kubectl's
# context here is set once, by kind itself, against this container's own
# real dockerd, and stays correct for the container's lifetime.
echo "==> Step 4: bash kind/setup_kind_cluster.sh ${SREGYM_ARCH}"
retry_with_backoff 3 30 "kind cluster bring-up" \
    bash kind/setup_kind_cluster.sh "${SREGYM_ARCH}"

# ── Step 5: Push baked images straight into the kind nodes' containerd ────
# Uses the same prepull-images.sh "load" mode, now that the cluster exists.
# Any image that failed to bake (Step 3 warning) is skipped here and left to
# whatever live `kubectl apply` pulls it at runtime (e.g. metrics-server's
# own apply inside conductor.py::deploy_app, mitigated by Step 6 below).
echo "==> Step 5: kind load image-archive (baked images -> kind nodes)"
CALICO_VERSION="${CALICO_VERSION:-v3.27.0}" \
    bash "${REPO_ROOT}/.devcontainer/prepull-images.sh" load /opt/prepulled-images "${SREGYM_ARCH}" "${KIND_CLUSTER_NAME}" \
    || echo "⚠️  Some baked images failed to load into kind nodes; live pulls will cover the gap (see Step 6)." >&2

# ── Step 6: metrics-server readiness, with retry-with-backoff around the ──
# exact failure mode observed live this session (registry.k8s.io pull
# timeouts from sandboxed egress: "dial tcp 34.96.108.209:443: i/o timeout",
# one of two replicas affected — flaky/rate-limited egress, not a total
# outage). The image itself is pre-baked (Step 5); this step covers the
# residual risk that a *replacement* pod scheduled later still needs to pull
# (node affinity changes, pod eviction, etc.) and that metrics-server takes
# a few reconcile cycles to report Available=True even once its pod is
# Running. This step does not itself deploy metrics-server — the repo's own
# sregym/conductor/conductor.py::deploy_app() does that on first problem
# run — it only verifies the cluster is in a state where that deploy will
# succeed quickly, and demonstrates the exact retry command a human should
# run by hand if it doesn't.
echo "==> Step 6: verify cluster is ready for metrics-server's own deploy step"
verify_metrics_server_prereqs() {
    kubectl get nodes -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}' \
        | tr ' ' '\n' | grep -qv False
}
retry_with_backoff 5 20 "cluster node readiness (metrics-server prerequisite)" \
    verify_metrics_server_prereqs

echo ""
echo "======================================================================"
echo " postCreate complete."
echo "======================================================================"
echo ""
echo "If a live SREGym run still stalls on metrics-server image pulls"
echo "despite the pre-baked image (residual egress flakiness, per failure"
echo "mode (3) observed this session), the documented fallback is:"
echo ""
echo "  1. Bump the timeout in .env:"
echo "       WAIT_FOR_POD_READY_TIMEOUT=1800"
echo "     (already set as a containerEnv default in devcontainer.json)"
echo ""
echo "  2. Manually retry the metrics-server rollout wait:"
echo "       kubectl -n kube-system rollout status deployment/metrics-server --timeout=600s"
echo ""
echo "  3. If a specific replica is stuck Pending/ImagePullBackOff, check"
echo "     which node it's scheduled on and re-run just that pod's pull:"
echo "       kubectl -n kube-system get pods -l k8s-app=metrics-server -o wide"
echo "       kubectl -n kube-system delete pod -l k8s-app=metrics-server --field-selector=status.phase!=Running"
echo ""
echo "See .devcontainer/README.md for the full ALIVE/MITIGATED/UNMITIGATED accounting."
