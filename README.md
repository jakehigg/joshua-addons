# Joshua addons

> **Pre-release.** The interfaces, the settings, and the addon contract can
> change between commits. Read the release notes before you update.

Joshua is a personal AI agent. Its core runs from
[joshua-ai](https://github.com/jakehigg/joshua-ai). This repository holds the
optional addons.

An addon is one container that serves an MCP server over HTTP. You name it in
the `mcp` section of your `joshua.yaml` as a `type: http` upstream, and the
Joshua gateway calls it. The gateway keeps the credential and applies the
per-person tool policy, so an addon does not change what the agent is allowed to
do.

Joshua is complete without an addon. Run the addons you want, and no others.

## Run an addon

Start Joshua from `joshua-ai` first. Then, for each addon you want:

- **On one Docker host**, use the `docker-compose.yml` in `addons/<name>/`. Each
  addon starts and stops on its own.
- **On Kubernetes**, install the chart in `charts/joshua-addon` one time for
  each addon, with that addon's `values.yaml`.

Each addon has a `README.md` that says what it does and which settings it takes.

## Read more

- [docs/architecture.md](docs/architecture.md): what an addon is, and the
  trust model between an addon and the core of Joshua.
- [docs/install.md](docs/install.md): the install steps, for Docker Compose
  and for Kubernetes.
- [docs/adding-an-addon.md](docs/adding-an-addon.md): the contract for a new
  addon.

## Versions

One version covers the whole repository. The chart version, the chart
`appVersion`, and every release image tag are the same string, and one tag releases all
of them.

Each push to a branch also publishes the image of every addon, amd64 only,
tagged with the commit SHA and with `branch-<name>`. Use one to test a branch
before a release. A branch build is not a release, and it has no version.
[docs/install.md](docs/install.md), section "Run a branch build", shows how to
run one on Kubernetes and how to go back to a release.

## Contribute

[CONTRIBUTING.md](CONTRIBUTING.md) explains the fork and upstream setup, the
checks to run, and the writing standard. [CLAUDE.md](CLAUDE.md) holds the
conventions for people and agents who change the code.

## License

Joshua addons are free software under the GNU Affero General Public License,
version 3 or later. See [LICENSE](LICENSE). You may run, change, and share
them. If you change one and let other people use it over a network, you must
offer them the source of your version.
