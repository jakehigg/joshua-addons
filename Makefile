.PHONY: sync lint fmt test up up-dev down logs ps

# Most targets take ADDON=<name>, the directory name under addons/. They run
# compose against that addon's own compose files:
#
#   make up ADDON=hello        the released image, from addons/hello/docker-compose.yml
#   make up-dev ADDON=hello    a build of this checkout, plus the dev override
#   make down ADDON=hello
#   make logs ADDON=hello
#   make ps ADDON=hello
#
# ADDON must name a directory under addons/ that has a docker-compose.yml.
# When it does not, the target prints the available addon names and stops.

ADDON_COMPOSE := addons/$(ADDON)/docker-compose.yml
ADDON_DEV_COMPOSE := addons/$(ADDON)/docker-compose.dev.yml

define require_addon
	@if [ -z "$(ADDON)" ] || [ ! -f "$(ADDON_COMPOSE)" ]; then \
		echo "set ADDON to one of:"; \
		for f in addons/*/docker-compose.yml; do \
			[ -e "$$f" ] || continue; \
			basename "$$(dirname "$$f")"; \
		done; \
		exit 1; \
	fi
endef

sync:
	uv sync --all-packages

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:
	@for d in addons/*/tests; do \
		[ -d "$$d" ] || continue; \
		name=$$(basename "$$(dirname "$$d")"); \
		echo "==> pytest $$name"; \
		uv run pytest "$$d" || exit 1; \
	done

# Run one addon. The release image comes from the GitHub container registry;
# nothing is built. Set JOSHUA_ADDONS_VERSION in the addon's .env to take
# another release.
up:
	$(require_addon)
	docker compose -f $(ADDON_COMPOSE) up -d
	docker compose -f $(ADDON_COMPOSE) ps

# Run one addon from a build of this checkout, tagged :dev.
up-dev:
	$(require_addon)
	docker compose -f $(ADDON_COMPOSE) -f $(ADDON_DEV_COMPOSE) up -d --build
	docker compose -f $(ADDON_COMPOSE) ps

down:
	$(require_addon)
	docker compose -f $(ADDON_COMPOSE) down

logs:
	$(require_addon)
	docker compose -f $(ADDON_COMPOSE) logs -f

ps:
	$(require_addon)
	docker compose -f $(ADDON_COMPOSE) ps
