# backlog-mcp Helm chart

Deploys one authenticated `backlog-mcp` HTTP server and its SQLite database. The chart uses
Helm chart API v2 and works with Helm v3 and v4.

## Requirements

- Kubernetes 1.25 or later
- Helm 3.12 or later, or Helm 4
- A pre-created Secret containing Basic-auth accounts
- The immutable manifest digest of the application image
- A ReadWriteOnce volume when persistence is enabled

The server is intentionally a single replica with a `Recreate` strategy. SQLite does not support
multiple independent writers. The container listens on port 3009, stores its database at
`/data/backlog.sqlite3`, and does not serve the diagnostic console. It serves HTTP only; terminate
TLS at the ingress. Each HTTP MCP client must identify itself with a stable
`?computer=<client-name>` query parameter, with optional `&project=<slug>`; without `computer`,
the server records the client as `unknown`.

## OCI distribution

Published releases are available as `oci://ghcr.io/mosquito/charts/backlog-mcp`. CI derives the
published chart `version` and `appVersion` from the `vX.Y.Z` release tag. Before CI can publish,
configure the `ghcr.io/mosquito/backlog-mcp` image package and the chart package to grant
`mosquito/backlog-mcp` Actions write access (or inherit repository access).
CI uses only `GITHUB_TOKEN` with `packages: write`.

For a private package, first run `helm registry login ghcr.io` with a token that has
`read:packages`. Install a published chart by version:

```sh
helm upgrade --install backlog-mcp oci://ghcr.io/mosquito/charts/backlog-mcp \
  --version 0.32.1 \
  --namespace backlog
```

## Install

Create the Basic-auth Secret outside Helm. Its value must use backlog-mcp's shell-quoted account
list format. Do not put credentials in `values.yaml`, `--set`, or a Git-tracked values file. Set
`BACKLOG_MCP_IMAGE_DIGEST` to the manifest digest of the image you publish or import.

```sh
export BACKLOG_MCP_IMAGE_DIGEST=sha256:replace-with-the-image-manifest-digest
kubectl create namespace backlog
kubectl -n backlog create secret generic backlog-mcp-auth \
  --from-literal=BACKLOG_MCP_AUTH_BASIC="$BACKLOG_MCP_AUTH_BASIC"

helm upgrade --install backlog-mcp ./charts/backlog-mcp \
  --namespace backlog \
  --set auth.existingSecret=backlog-mcp-auth \
  --set image.digest="$BACKLOG_MCP_IMAGE_DIGEST"
```

When `auth.existingSecret` is empty, the chart expects a Secret named
`<release-fullname>-auth`. For a release named `backlog-mcp`, that name is `backlog-mcp-auth`.
Set `auth.existingSecretKey` if the data key is not `BACKLOG_MCP_AUTH_BASIC`.

## Cilium ingress with cert-manager

Create the same external Secret, then save the following as a separate, non-secret values file:

```yaml
# values-cilium.yaml
auth:
  existingSecret: backlog-mcp-auth

http:
  allowedOrigin: https://backlog.lab.kemsa.pw

ingress:
  enabled: true
  className: cilium
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    ingress.cilium.io/force-https: "enabled"
  hosts:
    - host: backlog.lab.kemsa.pw
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: backlog-lab-kemsa-pw-tls
      hosts:
        - backlog.lab.kemsa.pw
```

Install it with:

```sh
helm upgrade --install backlog-mcp ./charts/backlog-mcp \
  --namespace backlog \
  --values values-cilium.yaml \
  --set image.digest="$BACKLOG_MCP_IMAGE_DIGEST"
```

`http.allowedOrigin` permits only the exact external TLS origin while retaining the endpoint's
DNS-rebinding protection. The Cilium annotation redirects plaintext requests to HTTPS so Basic
credentials are not sent over HTTP. Keep `http.trustProxy=false`; enable it only when a trusted
reverse proxy validates browser Origins itself.

## Local images with containerd

For a local Kubernetes cluster using containerd, import the image into the cluster's containerd
namespace, then prevent Kubernetes from pulling it:

```sh
sudo k3s ctr images import backlog-mcp_0.32.1.tar
sudo k3s ctr images tag \
  ghcr.io/mosquito/backlog-mcp:local \
  "ghcr.io/mosquito/backlog-mcp@$BACKLOG_MCP_IMAGE_DIGEST"

helm upgrade --install backlog-mcp ./charts/backlog-mcp \
  --namespace backlog \
  --set image.repository=ghcr.io/mosquito/backlog-mcp \
  --set image.digest="$BACKLOG_MCP_IMAGE_DIGEST" \
  --set image.pullPolicy=Never
```

Use the image name and manifest digest reported by the import. The explicit digest alias is needed
because CRI resolves the chart's exact `repository@digest` reference even with pulls disabled. For
plain containerd, add `-n k8s.io` to the `ctr` commands. For kind or k3d, use that cluster's
image-load command instead.

## Storage and security

Persistence is enabled by default and creates a 1 Gi `ReadWriteOnce` PVC. Set
`persistence.storageClass`, `persistence.size`, or `persistence.existingClaim` to use your storage
policy. Chart-created claims use Helm's `keep` policy by default, so uninstalling the release does
not delete the PVC. Set `persistence.retain=false` only when uninstalling the release should delete
that claim. Delete a retained PVC manually when it is no longer needed; its StorageClass reclaim
policy still controls what happens to the PV. Setting `persistence.enabled=false` makes `/data` an
ephemeral `emptyDir`; this discards all backlog data when the Pod is replaced.

The Pod runs as UID/GID 65532, uses the RuntimeDefault seccomp profile, has no mounted service
account token, is explicitly non-privileged, drops all Linux capabilities, disallows privilege
escalation, and uses a read-only root filesystem. `/data` and `/tmp` are the only writable mounts.
The PVC's `fsGroup` is 65532 so the process can create the SQLite database. These security
contexts are fixed in the templates rather than configurable values. Object references, Secret
keys, booleans, numeric bounds, and image digests are also checked or safely rendered by the
templates. Legacy AppArmor and seccomp override annotations are rejected, so skipping values-schema
validation cannot inject or weaken the Pod specification.

The Deployment rolls when the configured Secret name or key changes. Kubernetes does not restart a
Pod when data in an external Secret changes; restart the Deployment after rotating credentials.

## Configuration

| Value | Default | Purpose |
| --- | --- | --- |
| `image.repository` | `ghcr.io/mosquito/backlog-mcp` | Application image repository. |
| `image.digest` | `""` | Required immutable application manifest digest, for example `sha256:...`. |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `auth.existingSecret` | `<release-fullname>-auth` | Existing Secret containing Basic-auth accounts. |
| `auth.existingSecretKey` | `BACKLOG_MCP_AUTH_BASIC` | Secret data key passed to the process. |
| `persistence.enabled` | `true` | Create or mount persistent SQLite storage. |
| `persistence.storageClass` | `""` | StorageClass; empty uses the cluster default. |
| `persistence.size` | `1Gi` | Requested PVC capacity. |
| `persistence.existingClaim` | `""` | Use an existing PVC instead of creating one. |
| `persistence.retain` | `true` | Keep a chart-created PVC on uninstall. |
| `http.allowedOrigin` | `""` | Exact external browser Origin allowed behind TLS termination. |
| `http.trustProxy` | `false` | Disable Origin validation only for a proxy that validates public Origins. |
| `service.port` | `80` | ClusterIP Service port targeting container port 3009. |
| `resources` | CPU/memory requests and memory limit | Container resource requirements and limits. |
| `probes` | TCP probes | Startup, readiness, and liveness timing. |
| `ingress.enabled` | `false` | Create a standard `networking.k8s.io/v1` Ingress. |
| `ingress.className` | `""` | IngressClass name, such as `cilium`. |
| `ingress.hosts` / `ingress.tls` | `[]` | Hosts, paths, and TLS secrets. |

The chart renders only immutable `repository@digest` references. Set `image.digest` to the
manifest digest of the published or imported image before installing. The JSON schema rejects
replica counts other than one, non-`Recreate` strategies,
non-ClusterIP Services, and weaker default security settings. Run
`helm show values ./charts/backlog-mcp` for the complete values file.

## Verify

```sh
helm test backlog-mcp --namespace backlog
```

The test performs only a TCP connection to the ClusterIP Service. It sends no credentials and does
not call an authenticated endpoint.
