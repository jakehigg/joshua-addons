# The joshua-addon chart

This chart deploys one addon. Install it once for each addon you run, with
the values file for that addon. The release name becomes the name of every
object, so two addons never collide in the same namespace.

## Install

```
helm install hello charts/joshua-addon -f addons/hello/values.yaml
```

## Values

| Key | Default | What it does |
|---|---|---|
| `replicaCount` | `1` | Pods for the addon Deployment. |
| `image.repository` | `""` | Required. The image for the addon, such as `ghcr.io/jakehigg/joshua-addons-hello`. |
| `image.tag` | `""` | Empty means the `appVersion` of this chart, the version the addon shipped with. |
| `image.pullPolicy` | `IfNotPresent` | When to pull the image. |
| `port` | `8000` | The port the addon serves MCP and `/healthz` on. |
| `env` | `{}` | Plain environment variables, as a map of `NAME: value`. |
| `envFrom` | `[]` | Raw `envFrom` entries (`configMapRef`, `secretRef`), rendered as given. |
| `existingSecret` | `""` | A Secret name. Every key in it becomes an environment variable. |
| `probes.enabled` | `true` | Turn the liveness and readiness probes off for an addon with no `/healthz`. |
| `probes.path` | `/healthz` | The probe path. |
| `probes.port` | `""` | Empty means the value in `port`. |
| `probes.initialDelaySeconds` | `10` | Wait before the first probe. |
| `probes.periodSeconds` | `10` | Time between probes. |
| `resources` | `{}` | CPU and memory requests and limits for the addon container. |
| `extraContainers` | `[]` | Extra containers, rendered next to the addon container, as given. |
| `service.extraPorts` | `[]` | Extra Service ports, beyond `port`, rendered as given. |
| `ingress.enabled` | `false` | Expose the addon outside the cluster. |
| `ingress.className` | `""` | The IngressClass to use. |
| `ingress.host` | `""` | Required when `ingress.enabled` is true. |
| `ingress.path` | `/` | The path this addon answers on. |
| `ingress.annotations` | `{}` | Ingress annotations, such as a cert-manager issuer. |
| `ingress.tls.enabled` | `false` | Terminate TLS at the Ingress. |
| `ingress.tls.secretName` | `""` | Empty means `<host>-tls`. |
| `persistence.enabled` | `false` | Give the addon its own PersistentVolumeClaim. |
| `persistence.existingClaim` | `""` | Mount this claim instead, and make no claim. A claim that another release owns, such as the joshua-ai data volume. |
| `persistence.size` | `1Gi` | Requested storage. |
| `persistence.storageClass` | `""` | Empty means the cluster default. |
| `persistence.accessModes` | `[ReadWriteOnce]` | Access modes for the claim. |
| `persistence.mountPath` | `/data` | Where the addon container mounts the claim. |
| `configFile.enabled` | `false` | Write `configFile.content` to a ConfigMap and mount it read-only. |
| `configFile.name` | `config.yaml` | The file name, and the ConfigMap key. |
| `configFile.content` | `""` | Contents of the file, as a string. |
| `configFile.mountPath` | `/etc/joshua-addon/config.yaml` | Where the addon container mounts the file. |

## Versions

The chart version, the chart `appVersion`, and every addon image tag in this
repository are the same string. Leave `image.tag` empty and the chart pulls
the image that shipped with it. To test a branch, set `image.tag` to a commit
SHA or to `branch-<name>`. Each push to a branch publishes those tags, amd64
only. [docs/install.md](../../docs/install.md) shows the ArgoCD Application
for a branch build. `scripts/check_chart_version.py` checks that
the chart and the `docker-compose.yml` for every addon agree.

## ArgoCD

One Application per addon. This example installs `hello` with the same
values as `addons/hello/values.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: joshua-addon-hello
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/jakehigg/joshua-addons
    targetRevision: v0.0.1
    path: charts/joshua-addon
    helm:
      valuesObject:
        image:
          repository: ghcr.io/jakehigg/joshua-addons-hello
  destination:
    server: https://kubernetes.default.svc
    namespace: joshua
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

An ApplicationSet fits a person who runs many addons: one generator entry per
addon, each with its own `valuesObject`.
