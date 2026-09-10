# Computer-backed renewal demo

This fixture is the smallest complete catalog that demonstrates both execution
modes from the Computer specification:

1. `contract_extract` runs deterministic JavaScript in `fast_workspace@1` when
   contract evidence arrives. It uses no Agent and no model.
2. `renewal_assessment` runs `renewal_analyst@1` nightly in the same reusable
   Computer resource contract, returning cited candidate risks through the
   derivation's normal `emit` boundary.
3. The MCP interface can start a separate durable answering session for the
   same account. The account is the stable data boundary; neither Computer is
   registered to it.

For local and CI execution, the Computer provider is `fake`. Change only the
Computer provider to `cloudflare` when wiring the deployment runtime; all exact
Program, Agent, policy, artifact, and derivation references remain unchanged.

The fixture intentionally includes the buried pricing promise that makes the
demo visible: an old meeting record promises a 12% renewal discount if uptime
falls below 99.95%. A later incident reports 99.91%. The deterministic Program
extracts the contract terms, while the Agent must connect the old promise to
the incident, retain the original citations, and leave a new commercial
commitment as a review-required proposal.

## Seeing it

The fixture is fifteen files and twenty-nine parts that reference each other, so
read it as a graph rather than as a directory:

```sh
make catalog-graph                                  # this fixture
make catalog-graph CATALOG=examples/gbrain_catalog  # any other one
```

That writes `catalog-graph.html`, a self-contained page with no server behind
it. Both execution modes are visible in one view: `renewal_evidence` triggers
`contract_extract`, which runs the Program in `fast_workspace@1`; the nightly
`renewal_assessment` runs the analyst Agent in `research_workspace@1`, whose
writeback lands the observations directly and holds the pricing proposal for
review. The Agent's toolset is drawn tool by tool, so what it may read, run, and
write back is visible without opening `toolsets/renewal.yaml` — and both Agent
versions appear, which is what keeping a superseded definition published looks
like. Click a part for the budgets, mount paths, and citation rules it actually
commits to. `uv run memseek catalog-graph --help` has the options.

## Running it

See [the complete setup README](../README.md) for prerequisites, first-run
commands, Cloudflare configuration, and troubleshooting.

From the repository root, with Docker Compose and `uv` installed:

```sh
make computer-demo                 # short walkthrough, interactive replies
make computer-demo SCRIPTED=1      # fixed replies, verified outcomes, then exit
make computer-demo ADVANCED=1      # full desk: journal, recall, fork, receipts
make computer-demo MODE=cloudflare # real Agent; starts Wrangler automatically
```

Local mode uses a deterministic HTTP stand-in and needs no model credentials.
The launcher creates a demo workspace, publishes this fixture with the Computer
provider set to `cloudflare` (the HTTP adapter), and configures matching secrets.
The source fixture stays pinned to `fake` for CI. Local execution simulates the
provider protocol; it does not run JavaScript in a sandbox or call a model.

Cloudflare mode requires Node.js, `npm ci` in `cloudflare/computer-runtime`,
Wrangler authentication, and `MEMSEEK_RUNTIME_SECRET` in its `.dev.vars`. Its
Workers AI calls are real and billable. Export `COMPUTER_RUNTIME_URL` and
`COMPUTER_RUNTIME_TOKEN` to use a deployed runtime instead. Local mode overrides
those inherited values with its own stand-in settings. `make computer-demo-cloudflare` remains an alias.

Success verifies extracted terms, a cited risk, a completed invocation, active
observations, and draft proposals requiring review. Local mode also checks two
answered pauses. A failed stage exits nonzero. The launcher stops its own runtime;
Docker services and data remain until you stop them with `docker compose down`.

See [the tutorial](../../docs/computer-renewal-example.md) for SDK usage, port
overrides, manual setup, and the advanced desk's story.
