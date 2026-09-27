# Architecture

An addon is one optional container. It serves one MCP server over streamable
HTTP, on port 8000. The core of Joshua is complete without an addon. A person
adds an addon only when they want its tools, and runs only the addons they
want.

## The trust model

An addon sits behind the joshua-ai gateway. The gateway holds the credential
for the addon, and it applies a per-person allowlist for the tools of the
addon. Core calls a tool only through the gateway. Core never reaches an
addon directly, and an addon never sees a credential for another upstream.

The gateway treats an addon as a `type: http` upstream. It proxies the call
over MCP streamable HTTP, to the `url` given in `joshua.yaml`, verbatim. An
optional `headers:` block on the upstream entry carries a bearer token to the
addon on every call.

## Compose topology

On one Docker host, an addon and the joshua-ai stack join one Docker network.
The addon publishes no port on the host. Only a container on the shared
network reaches it, so the network is the outer boundary of the addon. The
gateway reaches an addon at its compose service name, such as
`http://hello:8000/mcp`.

## Kubernetes topology

On Kubernetes, a person installs one Helm release for each addon, from the
generic `charts/joshua-addon` chart. The release name becomes the Service
name of the addon, so two addons in one namespace never collide. The gateway
reaches an addon at the cluster Service DNS name, such as
`http://hello.joshua.svc.cluster.local:8000/mcp`.

## An addon that runs a model

The `developer` addon is the only addon with two images, and the only addon
that starts a second container. The manager, `addons/developer/`, is the MCP
server that the gateway calls. For each task, it starts one worker, from
`addons/developer-worker/`. The worker runs a model with Bash over code from
a git host.

The addon treats the worker as hostile. The worker holds no Joshua secret:
no Claude token, no fleet token, and no memory access. By default, it can
connect to the manager and to no other address. The manager forwards its
Claude requests and its git traffic, and scans each diff before it opens a
pull request. `addons/developer/README.md` has the full security model.

Every other addon stays one container.

## One version for the chart and every image

The chart version, the chart `appVersion`, and every addon image tag in this
repository are one string. A `v*` tag releases the chart and every addon image
together, so a chart never meets an image it did not ship with, and an addon
that did not change still gets the new tag.

A push to a branch also publishes every addon image, tagged with the commit
SHA and with `branch-<name>`. That image is for a test. It is not a release
and has no version.
