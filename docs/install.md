# Install an addon

An addon runs next to a joshua-ai stack, never alone. Pick the path that
matches how you run joshua-ai: Docker Compose or Kubernetes.

## Docker Compose

### Prerequisites

- A running joshua-ai stack, on the same Docker host.

### Procedure

This procedure installs the `hello` addon. For another addon, read its
`README.md` and use its name in place of `hello`.

1. Clone this repository and enter it.

```
git clone https://github.com/jakehigg/joshua-addons.git
cd joshua-addons
```

2. Copy the environment file of the addon.

```
cp addons/hello/.env.example addons/hello/.env
```

3. Start the addon.

```
make up ADDON=hello
```

The addon joins the Docker network that joshua-ai made. When joshua-ai is not
up, this step fails: start joshua-ai first.

4. Add the addon to the `mcp:` section of the `joshua.yaml` of joshua-ai.

```yaml
mcp:
  hello:
    type: http
    url: http://hello:8000/mcp
    allow: all
    headers:
      Authorization: "Bearer ${HELLO_ADDON_TOKEN:-}"
```

5. Set the token. Put the same value in `ADDON_TOKEN` in
   `addons/hello/.env`, and in `HELLO_ADDON_TOKEN` in the `.env` of
   joshua-ai. Restart the addon so it reads the new value:

```
make up ADDON=hello
```

6. Apply the config change. Run these commands from the joshua-ai directory.
   `POST /admin/reload` on the gateway reads the new entry and starts the
   connection. `core` also needs a restart to pick up the new tool.

```
export $(grep JOSHUA_TOKEN_LAPTOP .env)
docker compose exec -e TOKEN="$JOSHUA_TOKEN_LAPTOP" core python -c '
import os, urllib.request
req = urllib.request.Request(
    "http://gateway:8000/admin/reload", method="POST",
    headers={"Authorization": "Bearer " + os.environ["TOKEN"]})
print(urllib.request.urlopen(req).read().decode())
'
docker compose restart core
```

   This two-step apply is what the operations guide of joshua-ai states for
   a change to `mcp:`. Verify it yourself against
   `../joshua-ai/docs/operations.md`, section "Change the config", before you
   rely on it.

7. Verify. The joshua-ai compose file publishes no host port for the
   gateway, so reach it from inside the `core` container.

```
docker compose exec core python -c "import urllib.request; print(urllib.request.urlopen('http://gateway:8000/readyz').read().decode())"
```

   The answer carries a `connected` count for the MCP upstreams. The count
   goes up by one after step 6.

## Kubernetes

### Install

Clone this repository at the release tag you want, then install the chart:

```
git clone https://github.com/jakehigg/joshua-addons.git
cd joshua-addons
git checkout v0.0.1
helm install hello charts/joshua-addon -f addons/hello/values.yaml
```

The release name, `hello` above, becomes the Service name of the addon, and
the name of every object the chart makes. Install the chart again, with
another release name and another values file, for a second addon. The full
values table is in [the chart README](../charts/joshua-addon/README.md).

### ArgoCD

Run one Application for each addon. This example installs `hello`:

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

A person who runs many addons can use an ApplicationSet instead: one
generator entry per addon, each with its own `valuesObject`.

### Point the gateway at the addon

The gateway reaches an addon at the cluster Service DNS name, in the form
`http://<release>.<namespace>.svc.cluster.local:8000/mcp`. For the
`Application` above, that is `hello.joshua`:

```yaml
mcp:
  hello:
    type: http
    url: http://hello.joshua.svc.cluster.local:8000/mcp
    allow: all
    headers:
      Authorization: "Bearer ${HELLO_ADDON_TOKEN:-}"
```

Put this in the `joshua.yaml` that the chart of joshua-ai renders, then
reload the gateway. See the Docker Compose steps above for how the gateway
applies a change to `mcp:`. The same two-step apply holds on Kubernetes,
through `kubectl exec` in place of `docker compose exec`.

### Secrets

Set the token on both sides:

- **The addon.** Give it a Secret with the key `ADDON_TOKEN`, and name that
  Secret in the `values.yaml` of the addon, as `existingSecret`. Every key
  in the Secret becomes an environment variable in the addon container.
- **The gateway.** Add `HELLO_ADDON_TOKEN: {}` under `secrets.gateway.keys`
  in the joshua-ai values file, pointed at a Secret that holds the same
  token. See `charts/joshua/values.yaml` in joshua-ai for the full shape of
  `secrets`.
