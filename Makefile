UV ?= uv
COMPOSE ?= docker compose
DOCS_ADDR ?= 127.0.0.1:8001
WORKSPACE ?= local
COMPOSE_TEST_DATABASE_URL := postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
TEST_DATABASE_URL ?= $(COMPOSE_TEST_DATABASE_URL)

COMPUTER_RUNTIME_PORT ?= 8799
COMPUTER_RUNTIME_SECRET ?= local-computer-demo

.PHONY: help sync format lint typecheck build docs docs-build reference database database-down migrate migration-current quickstart up down logs check test e2e computer-demo computer-demo-cloudflare cloudflare-agent-smoke cloudflare-agent-smoke-local cloudflare-agent-smoke-setup

help:
	@echo "up             Run the whole local stack in Docker: postgres, api, worker, catalog"
	@echo "tools          Print the MCP tools the published catalog offers an agent"
	@echo "computer-demo  Run the Computer-backed renewal demo against the Docker stack"
	@echo "computer-demo-cloudflare  The same demo, driven by a real Cloudflare Agent"
	@echo "cloudflare-agent-smoke  Run a real Workers AI Agent against one durable Computer"
	@echo "cloudflare-agent-smoke-local  Run the same canary against wrangler dev, nothing deployed"
	@echo "cloudflare-agent-smoke-setup  Ask for runtime settings and prepare the live canary"
	@echo "down           Stop the local stack (add CLEAN=1 to delete its data and key)"
	@echo "logs           Follow the local stack's logs"
	@echo "sync           Install the frozen project and development dependencies"
	@echo "quickstart     Start the test database, apply the schema, and mint a workspace key"
	@echo "format         Format and apply safe lint fixes"
	@echo "lint           Check formatting and lint rules"
	@echo "typecheck      Type-check src/ and tests/ with ty"
	@echo "docs           Serve the MkDocs site at http://127.0.0.1:8001"
	@echo "docs-build     Build the MkDocs site into ./site"
	@echo "database       Start the isolated PostgreSQL 16 + pgvector test service"
	@echo "database-down  Remove the isolated database service"
	@echo "migrate        Upgrade the database to Alembic head using DATABASE_URL"
	@echo "migration-current  Show the database's current Alembic revision"
	@echo "check          Run all checks against an already available test database"
	@echo "test           Start an isolated database when needed and run all checks"
	@echo "e2e            Start an isolated database and run the ingest/search smoke test"

sync:
	$(UV) sync --frozen --all-groups

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint:
	$(UV) run ruff format --check .
	$(UV) run ruff check .

typecheck:
	$(UV) run ty check

build:
	$(UV) build

docs:
	$(UV) run --only-group dev mkdocs serve --dev-addr $(DOCS_ADDR)

docs-build:
	$(UV) run --only-group dev mkdocs build --strict

reference:
	cmp -s spec/reference.py examples/reference.py
	$(UV) run python examples/reference.py

# The whole local stack in Docker. `--wait` blocks until the API is healthy and
# the one-shot setup container has exited, so when this returns the MCP surface
# is already serving and .memseek/api_key exists.
up:
	@set -eu; \
	if ! $(COMPOSE) up -d --build --wait; then \
		printf '\n%s\n' "The stack did not come up. The setup step said:"; \
		$(COMPOSE) logs setup --no-log-prefix | tail -5; \
		exit 1; \
	fi; \
	: "--wait returns once containers are running; the one-shot setup step is"; \
	: "still publishing at that point, so block on it explicitly."; \
	if ! $(COMPOSE) wait setup >/dev/null; then \
		printf '\n%s\n' "Setup failed:"; \
		$(COMPOSE) logs setup --no-log-prefix | tail -5; \
		exit 1; \
	fi; \
	: "Both one-shot containers have exited 0 and every failure path above has"; \
	: "already read their logs, so nothing is left to learn from them. Compose"; \
	: "cannot auto-remove a service container, so do it here: what survives is"; \
	: "what is actually serving. Cosmetic, hence never fatal."; \
	$(COMPOSE) rm -fs migrate setup >/dev/null 2>&1 || true; \
	printf '\n%s\n' "Memseek is up:"; \
	printf '  %s\n' "API  http://127.0.0.1:$${MEMSEEK_PORT:-8000}"; \
	printf '  %s\n' "MCP  http://127.0.0.1:$${MEMSEEK_PORT:-8000}/mcp"; \
	printf '\n%s\n' "Next:"; \
	printf '  %s\n' "export MEMSEEK_URL=http://127.0.0.1:$${MEMSEEK_PORT:-8000}"; \
	printf '  %s\n' 'export MEMSEEK_API_KEY=$$(cat .memseek/api_key)'

# The Computer demo. A Computer is where Programs and Agents actually run, so
# the stack needs a runtime to call: this target brings the database, API, and
# worker up with COMPUTER_RUNTIME_* set (compose recreates api and worker when
# they change), then runs the example, which serves that runtime itself. The
# containers reach it at host.docker.internal; the example binds every interface
# for that reason.
#
# Deliberately not `up`: the demo mints its own disposable workspace over the
# exposed database port and publishes its own catalog, so it needs neither the
# `local` workspace nor the setup step that installs one — and an unrelated
# problem there must not stand between you and the demo.
computer-demo:
	@set -eu; \
	COMPUTER_RUNTIME_URL="http://host.docker.internal:$(COMPUTER_RUNTIME_PORT)" \
	COMPUTER_RUNTIME_TOKEN="$(COMPUTER_RUNTIME_SECRET)" \
	$(COMPOSE) up -d --build --wait api worker; \
	printf '\n%s\n' "Stack up. Serving the Computer runtime on port $(COMPUTER_RUNTIME_PORT)."; \
	DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:$${MEMSEEK_DB_PORT:-5433}/memseek" \
	COMPUTER_RUNTIME_URL="http://host.docker.internal:$(COMPUTER_RUNTIME_PORT)" \
	COMPUTER_RUNTIME_TOKEN="$(COMPUTER_RUNTIME_SECRET)" \
	MEMSEEK_BASE_URL="http://127.0.0.1:$${MEMSEEK_PORT:-8000}" \
	$(UV) run python examples/computer_renewal.py

# The same renewal desk, driven by a real Cloudflare Agent instead of the
# deterministic stand-in: Workers AI picks the steps, calls the tools, and
# decides for itself when to stop and ask you something. `wrangler dev` binds
# the remote AI binding, so those turns are real and billable.
#
# The secret comes from cloudflare/computer-runtime/.dev.vars so that the api,
# the worker, and the demo all sign with what the Worker verifies.
computer-demo-cloudflare:
	@set -eu; \
	secret=$$(sed -n 's/^MEMSEEK_RUNTIME_SECRET=//p' cloudflare/computer-runtime/.dev.vars | tr -d "\"'"); \
	[ -n "$$secret" ] || { printf 'error: MEMSEEK_RUNTIME_SECRET missing from cloudflare/computer-runtime/.dev.vars\n' >&2; exit 1; }; \
	COMPUTER_RUNTIME_URL="http://host.docker.internal:$(COMPUTER_RUNTIME_PORT)" \
	COMPUTER_RUNTIME_TOKEN="$$secret" \
	$(COMPOSE) up -d --build --wait api worker; \
	printf '\n%s\n' "Stack up. Start the Worker in another terminal, then this returns:"; \
	printf '%s\n\n' "  cd cloudflare/computer-runtime && npx wrangler dev --port $(COMPUTER_RUNTIME_PORT)"; \
	DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:$${MEMSEEK_DB_PORT:-5433}/memseek" \
	COMPUTER_RUNTIME_URL="http://127.0.0.1:$(COMPUTER_RUNTIME_PORT)" \
	COMPUTER_RUNTIME_TOKEN="$$secret" \
	MEMSEEK_BASE_URL="http://127.0.0.1:$${MEMSEEK_PORT:-8000}" \
	$(UV) run python examples/computer_renewal.py --cloudflare

# A live, database-free deployment canary. Settings reads these from the
# environment or the repository's untracked .env:
#   COMPUTER_RUNTIME_URL=https://<worker>.workers.dev
#   COMPUTER_RUNTIME_TOKEN=<MEMSEEK_RUNTIME_SECRET>
# The command performs two model turns on one session: write proof.json, then
# reopen it from a fresh task. The Python validator requires the corresponding
# tool events, file diff, citations, hashes, and worker-javascript receipt.
cloudflare-agent-smoke:
	$(UV) run python -m memseek.cloudflare_smoke

# The same canary without a deploy: `wrangler dev` serves the Worker locally
# with the secret from cloudflare/computer-runtime/.dev.vars. The AI binding is
# always remote, so the Workers AI turns are still real and still billable.
cloudflare-agent-smoke-local:
	bash scripts/run_cloudflare_smoke_local.sh

cloudflare-agent-smoke-setup:
	bash scripts/setup_cloudflare_smoke.sh

# CLEAN=1 also removes the volume and the minted key — a real fresh start.
down:
	@set -eu; \
	if [ -n "$(CLEAN)" ]; then \
		$(COMPOSE) down -v; \
		rm -rf .memseek; \
		echo "stack removed, data volume deleted, .memseek cleared"; \
	else \
		$(COMPOSE) down; \
		echo "stack stopped; data kept (make down CLEAN=1 to delete it)"; \
	fi

logs:
	$(COMPOSE) logs -f api worker

# What an agent will actually be offered, printed before you connect one. Runs
# inside the api container so Docker stays the only requirement; the key comes
# from the file `make up` wrote.
tools:
	@set -eu; \
	if [ ! -f .memseek/api_key ]; then \
		echo "no .memseek/api_key — run 'make up' first" >&2; \
		exit 1; \
	fi; \
	$(COMPOSE) exec \
		-e MEMSEEK_URL=http://127.0.0.1:8000 \
		-e MEMSEEK_API_KEY="$$(cat .memseek/api_key)" \
		api memseek mcp --check

database:
	$(COMPOSE) up -d --wait postgres-test

database-down:
	$(COMPOSE) rm --stop --force postgres-test

migrate:
	$(UV) run memseek migrate

migration-current:
	$(UV) run alembic current --check-heads

# One command from a clean checkout to a usable stack: database, schema, and a
# workspace credential. The API and worker stay in the foreground of their own
# terminals, so they are deliberately left to the caller.
quickstart:
	@set -eu; \
	$(COMPOSE) up -d --wait postgres-test; \
	DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run memseek migrate; \
	printf '\n%s\n' "Workspace '$(WORKSPACE)' — the api_key below is printed once:"; \
	DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run memseek create-workspace '$(WORKSPACE)'; \
	printf '\n%s\n' "Next: export MEMSEEK_API_KEY=<api_key>, then start the API and worker."

check: sync lint typecheck build reference
	LLM_FAKE=1 DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run pytest

test:
	@set -eu; \
	db_name='$(TEST_DATABASE_URL)'; \
	db_name=$${db_name##*/}; \
	db_name=$${db_name%%\?*}; \
	case "$$db_name" in *test*) ;; *) \
		echo "Refusing to run tests against non-test database: $$db_name" >&2; \
		exit 2; \
	esac; \
	started=0; \
	if [ "$(TEST_DATABASE_URL)" = "$(COMPOSE_TEST_DATABASE_URL)" ]; then \
		$(COMPOSE) up -d --wait postgres-test; \
		started=1; \
	fi; \
	cleanup() { \
		if [ "$$started" -eq 1 ]; then \
			$(COMPOSE) rm --stop --force postgres-test >/dev/null; \
		fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	$(MAKE) check TEST_DATABASE_URL='$(TEST_DATABASE_URL)'

e2e:
	@set -eu; \
	db_name='$(TEST_DATABASE_URL)'; \
	db_name=$${db_name##*/}; \
	db_name=$${db_name%%\?*}; \
	case "$$db_name" in *test*) ;; *) \
		echo "Refusing to run e2e against non-test database: $$db_name" >&2; \
		exit 2; \
	esac; \
	started=0; \
	if [ "$(TEST_DATABASE_URL)" = "$(COMPOSE_TEST_DATABASE_URL)" ]; then \
		$(COMPOSE) up -d --wait postgres-test; \
		started=1; \
	fi; \
	cleanup() { \
		if [ "$$started" -eq 1 ]; then \
			$(COMPOSE) rm --stop --force postgres-test >/dev/null; \
		fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	LLM_FAKE=1 DATABASE_URL="$(TEST_DATABASE_URL)" $(UV) run pytest tests/test_e2e.py -q
