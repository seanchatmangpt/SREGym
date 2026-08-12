#!/usr/bin/env bash
# prepull-images.sh
#
# Pre-populates the exact images that this session's live SREGym runs stalled
# or failed on (failure mode 3: metrics-server pull timeouts; failure modes
# 1/2's kind node image + Calico image set, pulled proactively so a fresh
# devcontainer never has to fetch them cold on first `kind create cluster`).
#
# Two modes, because a docker daemon is NOT available at `docker build` time
# (dockerd only starts once the docker-in-docker feature's runtime container
# boots) but IS available once the devcontainer is actually running:
#
#   bake <out_dir>   - build-time. No daemon required. Uses `skopeo copy` to
#                       pull each image straight to a docker-archive tarball
#                       under <out_dir>. Called from the Dockerfile.
#   load <out_dir>   - runtime (postCreate). Daemon required. `docker load`s
#                       each baked tarball into the devcontainer's own daemon,
#                       then (once the kind cluster exists) `kind load
#                       image-archive`s each tarball straight into every kind
#                       node's containerd, so no node ever pulls these over
#                       the network at all.
#
# Arch is passed explicitly (arm|x86) rather than re-detected here, so both
# modes agree with whatever kind/setup_kind_cluster.sh's own `uname -m`
# autodetect in postCreate.sh resolved.

set -euo pipefail

MODE="${1:?Usage: prepull-images.sh <bake|load> <out_dir> [arch] [kind_cluster_name]}"
OUT_DIR="${2:?Usage: prepull-images.sh <bake|load> <out_dir> [arch] [kind_cluster_name]}"
ARCH="${3:-x86}"
KIND_CLUSTER_NAME="${4:-kind}"
CALICO_VERSION="${CALICO_VERSION:-v3.27.0}"

mkdir -p "${OUT_DIR}"

if [[ "${ARCH}" == "arm" ]]; then
    KIND_NODE_IMAGE="jacksonarthurclark/aiopslab-kind-arm:latest"
else
    KIND_NODE_IMAGE="jacksonarthurclark/aiopslab-kind-x86:latest"
fi

# Calico's image set, per the manifest applied by kind/setup_kind_cluster.sh
# (`calico.yaml` for this exact pinned CALICO_VERSION). These four are the
# images that manifest's DaemonSet/Deployment specs actually reference.
CALICO_IMAGES=(
    "docker.io/calico/cni:${CALICO_VERSION}"
    "docker.io/calico/node:${CALICO_VERSION}"
    "docker.io/calico/kube-controllers:${CALICO_VERSION}"
    "docker.io/calico/pod2daemon-flexvol:${CALICO_VERSION}"
)

# metrics-server: sregym/conductor/conductor.py's own deploy_app() applies
# "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
# unpinned by the upstream repo's own design (always "latest"). We cannot cite
# a fixed tag from THIS repo because the repo itself never pins one — so we
# resolve the exact image reference the *current* latest release's manifest
# contains, at build time, and bake exactly that resolved tag. This is
# reproducible per-build (the resolved tag is printed and baked into the
# image name on disk) even though upstream's pointer keeps moving.
resolve_metrics_server_image() {
    curl -fsSL "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml" \
        | grep -m1 -oE 'image: registry\.k8s\.io/metrics-server/metrics-server:[^[:space:]]+' \
        | awk '{print $2}'
}

image_to_filename() {
    # registry/repo/name:tag -> registry_repo_name_tag.tar
    echo "$1" | tr '/:' '__' | tr -cd '[:alnum:]_.-'
}

bake() {
    local metrics_server_image
    metrics_server_image="$(resolve_metrics_server_image)"
    if [[ -z "${metrics_server_image}" ]]; then
        echo "⚠️  Could not resolve metrics-server image from upstream release manifest at build time." >&2
        echo "    Skipping bake for metrics-server; postCreate.sh's retry-with-backoff wrapper is the fallback." >&2
    fi

    local all_images=("${KIND_NODE_IMAGE}" "${CALICO_IMAGES[@]}")
    [[ -n "${metrics_server_image:-}" ]] && all_images+=("${metrics_server_image}")

    # Record exactly which images were baked and at which reference, so
    # postCreate.sh (and a human debugging staleness later) never has to
    # guess what's actually on disk.
    : > "${OUT_DIR}/manifest.txt"

    for image in "${all_images[@]}"; do
        local fname
        fname="$(image_to_filename "${image}").tar"
        echo "==> [bake] skopeo copy docker://${image} -> ${OUT_DIR}/${fname}"
        if skopeo copy --retry-times 5 "docker://${image}" "docker-archive:${OUT_DIR}/${fname}:${image}"; then
            echo "${image}	${fname}" >> "${OUT_DIR}/manifest.txt"
        else
            echo "⚠️  skopeo copy failed for ${image}; leaving it for the runtime retry path in postCreate.sh." >&2
        fi
    done

    echo "==> [bake] done. $(wc -l < "${OUT_DIR}/manifest.txt" 2>/dev/null || echo 0) image(s) baked into ${OUT_DIR}."
}

load() {
    if [[ ! -f "${OUT_DIR}/manifest.txt" ]]; then
        echo "⚠️  No bake manifest at ${OUT_DIR}/manifest.txt — nothing was baked at build time, or bake failed entirely." >&2
        return 0
    fi

    while IFS=$'\t' read -r image fname; do
        [[ -z "${image}" ]] && continue
        local archive="${OUT_DIR}/${fname}"
        if [[ ! -f "${archive}" ]]; then
            echo "⚠️  Missing baked archive for ${image} (${archive}); skipping." >&2
            continue
        fi
        echo "==> [load] docker load < ${archive}"
        docker load -i "${archive}"

        echo "==> [load] kind load image-archive ${archive} --name ${KIND_CLUSTER_NAME}"
        # kind load image-archive works directly from a docker-save-format
        # tarball and pushes straight into every node's containerd — this is
        # the step that means no kind node ever hits the network for these
        # images at all, structurally, not just "usually cached."
        kind load image-archive "${archive}" --name "${KIND_CLUSTER_NAME}"
    done < "${OUT_DIR}/manifest.txt"

    echo "==> [load] done."
}

case "${MODE}" in
    bake) bake ;;
    load) load ;;
    *) echo "Unknown mode: ${MODE} (expected bake|load)" >&2; exit 1 ;;
esac
