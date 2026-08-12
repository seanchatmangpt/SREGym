# syntax=docker/dockerfile:1.7

# Pinned by digest, not a floating "latest"/"ubuntu-22.04" tag — resolved live
# this session with `docker pull mcr.microsoft.com/devcontainers/base:ubuntu-22.04`
# then `docker manifest inspect` on that tag; re-pull and update this digest
# deliberately (never silently) when a base-image refresh is needed.
FROM mcr.microsoft.com/devcontainers/base@sha256:81380e4c9c14e8a629ff39029639e4b7893e67400246fa7782a0fe7dc193a02a

ARG KIND_VERSION=v0.27.0
ARG HELM_VERSION=v4.0.0
ARG PYTHON_VERSION=3.12
ARG CALICO_VERSION=v3.27.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ── Base tooling ────────────────────────────────────────────────────────────
# skopeo pulls/saves container images to docker-archive tarballs WITHOUT a
# running docker daemon, which is what makes baking prepulled images into
# this build possible at all: `docker build` has no dockerd to talk to
# (dockerd only exists once the docker-in-docker feature starts the
# container at runtime), so `docker pull` cannot run here — skopeo can.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg lsb-release jq skopeo software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# ── Python 3.12 (deadsnakes, matching docker/agents/Dockerfile's own approach
# and this repo's requires-python >= 3.12 / .python-version pin) ───────────
RUN add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1

# ── uv ───────────────────────────────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /usr/local/bin/

# ── kubectl (pinned to the kind node image's own server version family; the
# CLI is forward/backward compatible within one minor version so pin to a
# recent stable release rather than guessing the exact node build) ────────
RUN KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable-1.30.txt)" \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# ── kind (matches .github/workflows/smoke-test.yml's own pin exactly) ─────
RUN curl -fsSLo /usr/local/bin/kind "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-$(dpkg --print-architecture)" \
    && chmod +x /usr/local/bin/kind

# ── Helm (README.md's own stated requirement: >= 4.0) ──────────────────────
RUN curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-$(dpkg --print-architecture).tar.gz" -o /tmp/helm.tgz \
    && tar -xzf /tmp/helm.tgz -C /tmp \
    && mv "/tmp/linux-$(dpkg --print-architecture)/helm" /usr/local/bin/helm \
    && rm -rf /tmp/helm.tgz "/tmp/linux-$(dpkg --print-architecture)"

# ── Bake the images this session's failure mode (3) actually stalled on ────
# Baked as docker-archive tarballs at BUILD time (baked layers), not fetched
# at postCreate/runtime. postCreate.sh's own retry-with-backoff wrapper
# exists only for the residual, non-bakeable runtime pulls (see that file
# and README.md's rationale). All of this runs without a docker daemon,
# via skopeo, which is the whole point: `docker build` has none.
COPY .devcontainer/prepull-images.sh /opt/prepull-images.sh
RUN chmod +x /opt/prepull-images.sh \
    && mkdir -p /opt/prepulled-images \
    && CALICO_VERSION="${CALICO_VERSION}" /opt/prepull-images.sh bake /opt/prepulled-images

# ── Non-root user matches the docker-in-docker feature's own remapped user ─
ARG USERNAME=vscode
RUN mkdir -p /home/${USERNAME}/cache_dir && chown -R ${USERNAME}:${USERNAME} /home/${USERNAME} /opt/prepulled-images

USER ${USERNAME}
WORKDIR /workspaces/SREGym
