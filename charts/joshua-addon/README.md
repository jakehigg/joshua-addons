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
| `extraEnv` | `[]` | Raw `env` entries, such as a `valueFrom` `fieldRef`, rendered as given after `env`. |
| `envFrom` | `[]` | Raw `envFrom` entries (`configMapRef`, `secretRef`), rendered as given. |
| `existingSecret` | `""` | A Secret name. Every key in it becomes an environment variable. |
| `probes.enabled` | `true` | Turn the liveness and readiness probes off for an addon with no `/healthz`. |
| `probes.path` | `/healthz` | The probe path. |
| `probes.port` | `""` | Empty means the value in `port`. |
| `probes.initialDelaySeconds` | `10` | Wait before the first probe. |
| `probes.periodSeconds` | `10` | Time between probes. |
| `resources` | `{}` | CPU and memory requests and limits for the addon container. |
| `serviceAccount.create` | `false` | Make a ServiceAccount for the release. `rbac.enabled` makes one too. |
| `serviceAccount.name` | `""` | The ServiceAccount name. Empty means the release name when the chart makes one, else the namespace default. |
| `extraContainers` | `[]` | Extra containers, rendered next to the addon container, as given. |
| `service.extraPorts` | `[]` | Extra Service ports, beyond `port`, rendered as given. |
| `ingress.enabled` | `false` | Expose the addon outside the cluster. |
| `ingress.className` | `""` | The IngressClass to use. |
| `ingress.host` | `""` | Required when `ingress.enabled` is true. |
| `ingress.path` | `/` | The path this addon answers on. |
| `ingress.annotations` | `{}` | Ingress annotations, such as a cert-manager issuer. |
| `ingress.tls.enabled` | `false` | Terminate TLS at the Ingress. |
| `ingress.tls.secretName` | `""` | Empty means `<host>-tls`. |
| `podSecurityContext` | `fsGroup: 1000` | The pod security context. The `fsGroup` lets the uid 1000 addon user write to a mounted volume. `{}` removes the block. |
| `strategy` | `""` | The Deployment update strategy. Empty means `Recreate` when the chart makes the claim, because that claim is ReadWriteOnce and a rolling update cannot attach it twice. With `existingClaim` the Kubernetes default stays. Set `Recreate` or `RollingUpdate` to choose. |
| `deploymentAnnotations` | `{}` | Annotations on the Deployment, such as `reloader.stakater.com/auto: "true"`, so a Reloader restarts the pod when its Secret changes. |
| `podAnnotations` | `{}` | Annotations on the pod template. With `configFile.enabled`, the chart adds `checksum/config` on its own, so a changed file restarts the pod. |
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
| `rbac.enabled` | `false` | Make a ServiceAccount, a Role, and a RoleBinding. See "RBAC". |
| `rbac.rules` | jobs, pods, `pods/log` | The rules of the Role. |
| `networkPolicy.enabled` | `false` | Make the two NetworkPolicies. See "NetworkPolicy". |
| `networkPolicy.workerSelector` | `joshua-addon: developer`, `app.kubernetes.io/name: joshua-addon-developer-worker` | The labels of the worker pods. |
| `networkPolicy.managerPorts` | `[8001, 8002]` | The addon ports that the workers call. |
| `networkPolicy.allowDns` | `true` | Let a worker use DNS on port 53, to find the addon Service by name. |
| `networkPolicy.workersInternetEgress` | `false` | Let a worker connect to public addresses. |
| `networkPolicy.clusterCidrs` | `[]` | The pod and Service CIDRs of the cluster. Workers cannot connect to them when `workersInternetEgress` is true. |
| `networkPolicy.ingressFrom` | `[]` | Raw `from` peers for `port`. Empty means every pod in `gatewayNamespaces`. |
| `networkPolicy.gatewayNamespaces` | `[]` | The namespaces that can call `port`. Empty means every namespace. |

## RBAC

Most addons do not call the Kubernetes API, and they do not need this block.
An addon that starts pods, such as `developer`, sets `rbac.enabled: true`.
The chart then makes:

- A ServiceAccount, with the release name. The addon pod runs as it.
- A Role in the release namespace, with `rbac.rules`. The default rules let
  the addon create, read, and delete Jobs, and read pods and pod logs.
- A RoleBinding from the Role to the ServiceAccount.

The Role gives no access to another namespace.

## NetworkPolicy

An addon that starts worker pods, such as `developer`, sets
`networkPolicy.enabled: true`. The chart then makes two policies:

- `<release>-workers` applies to the pods that match `workerSelector`. A
  worker can connect to the addon pod on `managerPorts`, and to port 53 when
  `allowDns` is true. It cannot connect to other addresses. With
  `workersInternetEgress: true`, a worker can also connect to all addresses
  except `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, and
  `clusterCidrs`. Set `clusterCidrs` to the pod and Service CIDRs of your
  cluster when they are not in these ranges.
- `<release>-manager` applies to the addon pod. The addon accepts `port`
  from `ingressFrom`, or from every pod in `gatewayNamespaces`. It accepts
  `managerPorts` from the worker pods only. The policy does not limit the
  connections that the addon makes.

The CNI of the cluster must enforce NetworkPolicy. If it does not, the
policies have no effect, and a worker can connect to all addresses.

## Versions

The chart version, the chart `appVersion`, and every addon image tag in this
repository are the same string. Leave `image.tag` empty and the chart pulls
the image that shipped with it. To test a branch, set `image.tag` to a commit
SHA or to `branch-<name>`. Each push to a branch publishes those tags, amd64
only. [docs/install.md](../../docs/install.md) shows the ArgoCD Application
for a branch build. `scripts/check_chart_version.py` checks that
the chart, the `docker-compose.yml` for every addon, and each pinned
`WORKER_IMAGE` tag agree.

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
