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

## Running it

`examples/computer_renewal.py` drives this catalog end to end — write-triggered
Program, nightly Agent derivation, and a durable invocation that pauses for a
human — and then hands you a prompt.

```sh
make computer-demo
```

That brings the Docker stack up with a Computer runtime configured and runs the
demo against it. Nothing else is required: no API keys (the model provider here
is `fake`, and neither Computer calls a model), and the demo mints its own
disposable workspace, so the `local` workspace `make up` sets up is untouched.

A Computer is where Programs and Agents actually run, so the stack needs a
runtime to call — and the demo is it. It serves a small stand-in for
`cloudflare/computer-runtime`: same signed wire protocol, same response
envelope, no sandbox and no model. It prints every request the worker sends it.
The containers reach it at `host.docker.internal:8799`, which is why it binds
every interface; override the port and shared secret with
`COMPUTER_RUNTIME_PORT` and `COMPUTER_RUNTIME_SECRET`.

Point `COMPUTER_RUNTIME_URL` at a deployed Worker instead and the demo detects
it, serves nothing, and drives the real runtime — the catalog is unchanged
either way. The demo publishes this directory with the one edit the deployment
guide names (`provider: fake` → `provider: cloudflare`), so it runs whichever way
the fixture is currently pinned.

Without Docker, run the same thing by hand. Put the runtime in `.env`, so the
API, the worker, and the demo agree on it:

```
COMPUTER_RUNTIME_URL=http://127.0.0.1:8799
COMPUTER_RUNTIME_TOKEN=local-demo-secret
```

```sh
make database && source .env.sh
uv run memseek migrate
uv run uvicorn memseek.api:app &                 # terminal A
uv run memseek worker &                          # terminal B
uv run python examples/computer_renewal.py       # terminal C
```
