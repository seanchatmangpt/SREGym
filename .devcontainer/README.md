# SREGym devcontainer

A hardened GitHub Codespaces / devcontainer configuration for running SREGym
(4-node kind cluster + Calico + the full SRE app stack) in a real, fresh
Linux VM, with no Colima or any other host-Docker-context layer in the path.

Files: `devcontainer.json`, `Dockerfile`, `postCreate.sh`,
`prepull-images.sh`, this `README.md`.

## What this structurally prevents vs. mitigates vs. cannot fully eliminate

This maps directly onto the three real failure modes observed live this
session running SREGym against a local kind cluster on macOS via Colima.

### (1) Colima resetting `kubectl config current-context` — structurally prevented

Colima's own docker-socket calls silently reset the active context back to
`colima`, which made the vendored gym's `KubernetesAPIProxy`
(`sregym/service/k8s_proxy.py`) resolve a stale, dead API-server host/port
from `~/.kube/config` and fail with `Connection refused`.

A Codespace/devcontainer runs in a real, dedicated Linux VM. Docker access
is provided by the pinned `ghcr.io/devcontainers/features/docker-in-docker`
feature, which runs a real `dockerd` **inside** this container — there is no
host Docker Desktop, no Colima, no second machine's docker context to
silently reset. `kubectl`'s context is set once by `kind create cluster`
against that one real daemon and never has a second, competing writer. There
is no code path by which this failure mode can occur here, not merely a
lower probability of it.

### (2) Wrongly-shaped / stale kind cluster — structurally prevented

A single-node, no-CNI kind cluster (left over from an earlier, different
`kind create` invocation) produced `kubectl-mcp-port-9954-unreachable`
failures and Calico CRD "resource type not found" errors, because the
cluster on disk didn't match what the tooling assumed was there.

`postCreate.sh` Step 2 unconditionally deletes any pre-existing cluster
named `kind` before rebuilding, every single time the container is created
or rebuilt, then Step 4 always brings the cluster up fresh via the repo's
own `kind/setup_kind_cluster.sh <arch>` — the exact same script
`.github/workflows/smoke-test.yml` runs in CI, using the exact same
`kind/kind-config-{arm,x86}.yaml` (4 nodes: 1 control-plane + 3 workers,
`disableDefaultCNI: true`, Calico applied explicitly at the pinned
`v3.27.0`). A devcontainer session can never inherit a stale or
wrongly-shaped cluster from a prior session, because there is no prior
session's cluster state to inherit — devcontainer rebuilds start from a
fresh container filesystem outside the one Docker volume the DinD feature
persists, and even that volume's cluster is explicitly deleted first.

### (3) metrics-server pull timeouts on flaky sandboxed egress — mitigated, not eliminated

Live evidence this session: `conductor.py::deploy_app` hung ~10 minutes on
"Setting up metrics-server…" because
`registry.k8s.io/metrics-server/metrics-server` pulled intermittently
timed out (`dial tcp 34.96.108.209:443: i/o timeout`) — one of two replicas
pulled fine, the other didn't. That's flaky/rate-limited egress from the
sandboxed VM, not a total outage, and Codespaces VMs have their own egress
path with its own (different, but not necessarily better) characteristics —
so this cannot be claimed eliminated, only substantially reduced.

**What's baked in (`Dockerfile` + `prepull-images.sh bake`, build time, no
runtime pull at all for these):**

- `jacksonarthurclark/aiopslab-kind-{arm,x86}:latest` (the kind node image;
  parameterized by arch, matching `kind/kind-config-*.yaml`)
- The Calico `v3.27.0` image set actually referenced by the manifest
  `kind/setup_kind_cluster.sh` applies:
  `calico/cni`, `calico/node`, `calico/kube-controllers`,
  `calico/pod2daemon-flexvol`, all at `v3.27.0`
- `registry.k8s.io/metrics-server/metrics-server`, at the **exact tag
  resolved from the repo's own deploy code's manifest URL** at build time.

  This needs a caveat, stated precisely rather than guessed away:
  `sregym/conductor/conductor.py::deploy_app()` applies
  `https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml`
  — upstream's own **unpinned "latest" pointer**, not a fixed tag this repo
  controls. There is no fixed tag in this repo to cite. `prepull-images.sh`
  resolves that pointer to a concrete image reference at build time (`grep`
  over the fetched manifest, same as reading it by hand) and bakes exactly
  that resolved tag, recording it in `/opt/prepulled-images/manifest.txt`
  inside the image so it's auditable, not silently assumed. If upstream's
  "latest" has moved between this image's build and a given run, the baked
  tag and the live-applied tag can diverge — Step 6 below is the coverage
  for that gap.

Baking uses `skopeo copy docker://IMAGE docker-archive:...tar` rather than
`docker pull` + `docker save`, because `docker build` has no running daemon
to pull *into* — the docker-in-docker feature's `dockerd` only exists once
the devcontainer is actually running, not during image build. `skopeo`
needs no daemon. At `postCreate` time (`prepull-images.sh load`), each
tarball is `docker load`ed into the running devcontainer's own daemon and
then `kind load image-archive`d directly into every kind node's containerd
— so, when the bake succeeded, no kind node ever touches the network for
these images at all.

**What's retried, not baked** (`postCreate.sh`'s `retry_with_backoff`,
exponential, logged per attempt):

- `uv sync`
- kind cluster bring-up (`kind/setup_kind_cluster.sh`, which already has its
  own internal Calico-rollout retry/diagnostics logic — the wrapper here
  covers transient failures in the surrounding `kind create` step itself)
- the node-readiness check gating metrics-server's own deploy step

**What can't be fully eliminated, and the documented fallback:**

Even with the image baked, a *replacement* metrics-server pod scheduled
later (node affinity change, pod eviction, cluster rebuild inside a
long-lived Codespace) can still need a live pull, and `deploy_app()`'s own
wait can still be slow on a bad network day. `postCreate.sh` prints this
fallback at the end of every run:

1. Bump `WAIT_FOR_POD_READY_TIMEOUT=1800` (30 min) — already set as a
   `containerEnv` default in `devcontainer.json`, per `kind/README.md`'s own
   documented recommendation for slow-network deployments.
2. Manually re-wait: `kubectl -n kube-system rollout status deployment/metrics-server --timeout=600s`
3. If a specific replica is stuck, identify and re-cycle just that pod:
   ```bash
   kubectl -n kube-system get pods -l k8s-app=metrics-server -o wide
   kubectl -n kube-system delete pod -l k8s-app=metrics-server --field-selector=status.phase!=Running
   ```

## Security posture

- **docker-in-docker, not Docker-outside-of-Docker**: `ghcr.io/devcontainers/features/docker-in-docker:2.12.2`,
  pinned to an exact feature version, `moby: true`. This runs a real,
  contained `dockerd` inside the devcontainer rather than bind-mounting
  `/var/run/docker.sock` from the host into a `--privileged` container
  (DooD), which would hand the container the host daemon's full ambient
  authority. Neither path is available in a Codespace anyway — there is no
  host daemon to bind-mount — but the explicit choice matters for anyone
  reusing this config against a local Docker Desktop devcontainer host too.
- **Non-root remoteUser**: `remoteUser`/`containerUser` are both `vscode`
  (`common-utils` feature, `userUid: 1000` / `userGid: 1000`), with
  `updateRemoteUserUID: true` so the ID maps onto whatever real UID/GID
  Codespaces' own user-namespace remapping assigns — the container never
  runs as root once `postCreate` hands off.
- **Secrets never baked, never committed**: `GROQ_API_KEY`,
  `ANTHROPIC_API_KEY`, `AGENT_API_KEY` are referenced in `containerEnv` as
  `${localEnv:...}` — i.e. read from whatever the Codespaces host already
  injected into the container's process environment from the user's/org's
  configured Codespaces secrets. No `ARG`/`ENV` in the `Dockerfile` sets any
  of them, and no `.env` file with real values is part of this devcontainer
  config. A user must configure these as Codespaces secrets (repo or org
  Settings → Secrets and variables → Codespaces) before first launch; if
  unset, the corresponding env var is simply empty inside the container.
- **Pinned base image digest**: `mcr.microsoft.com/devcontainers/base@sha256:81380e4c9c14e8a629ff39029639e4b7893e67400246fa7782a0fe7dc193a02a`
  — resolved live this session via
  `docker pull mcr.microsoft.com/devcontainers/base:ubuntu-22.04` followed by
  `docker manifest inspect`, not copied from memory or a guess. Update this
  digest deliberately (re-pull, re-inspect, re-paste) when a refresh is
  wanted; a floating tag is never used for the build's own base layer.
- **Pinned tool versions throughout**: `kind v0.27.0` (matches
  `.github/workflows/smoke-test.yml` exactly), `helm v4.0.0` (README.md's
  own stated `>= 4.0` requirement), Python 3.12 via deadsnakes (matches
  `pyproject.toml`'s `requires-python >= 3.12` and this repo's
  `.python-version`), `uv 0.7.13` pinned via a `COPY --from=` of the
  official `ghcr.io/astral-sh/uv` image rather than a curl-pipe-to-shell
  installer script.

## `hostRequirements` rationale (8 cpus / 16gb / 64gb storage)

Not a guess — derived from two real, named sources in this repo:

- `kind/README.md`'s own troubleshooting section documents this as a real
  4-node cluster (1 control-plane + 3 workers) running Calico plus, per
  `sregym/conductor/conductor.py::deploy_app`, metrics-server, Khaos,
  OpenEBS, Prometheus, Jaeger, and Loki concurrently — a materially heavier
  workload than kind's own single-node "4 CPU / 8GB" baseline guidance, and
  the README explicitly calls out WSL2 users needing to raise their VM's
  resource allocation for exactly this reason.
- `.github/workflows/smoke-test.yml` runs this exact same cluster
  bring-up + a mitigation-only problem run successfully on GitHub-hosted
  `ubuntu-latest` runners, which are a fixed, known class: 4 vCPU / 16GB RAM
  / 14GB SSD as of GitHub's current standard runner spec. That is this
  repo's own empirical existence proof that the stack *can* run in 4 vCPU /
  16GB — but CI there runs one mitigation-only problem with a fresh runner
  and no IDE/editor process competing for RAM, and does not need headroom
  for the baked-image layers this devcontainer adds on top (the four Calico
  images, the kind node image, and metrics-server, baked directly into the
  image's own filesystem in addition to what containerd will unpack them
  into again once loaded).

  8 cpus / 16gb keeps the CPU floor CI already proves works, while adding a
  materially larger `storage` allocation (64gb vs. CI's ~14GB) to
  accommodate the baked image layers plus the cluster's own container
  storage without the two competing for space — the specific, concrete
  gap between "CI proved this works once, disposably" and "a developer
  will live in this Codespace across multiple cluster rebuilds."
