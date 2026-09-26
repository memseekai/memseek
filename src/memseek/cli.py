"""Operational command-line interface for migrations, workspace setup, and workers."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from memseek.auth import create_workspace
from memseek.config import Settings, get_settings
from memseek.db import pool_lifespan
from memseek.logging import configure_logging
from memseek.migrations import apply_migrations
from memseek.worker import run_worker


def build_parser() -> argparse.ArgumentParser:
    """Construct the operational command tree."""

    parser = argparse.ArgumentParser(prog="memseek")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="upgrade the database to Alembic head")
    create = subparsers.add_parser("create-workspace", help="create a workspace API key")
    create.add_argument("workspace")
    subparsers.add_parser("worker", help="run the asynchronous worker process")
    retry = subparsers.add_parser("retry-job", help="requeue one dead job")
    retry.add_argument("job_id")
    reindex = subparsers.add_parser("reindex", help="rebuild external search projections")
    reindex.add_argument("--workspace", required=True)
    reindex.add_argument("--since-seq", type=int)
    reindex.add_argument("--reset", action="store_true")
    reindex.add_argument("--yes", action="store_true", help="confirm reset outside test databases")
    for name, help_text in (
        ("catalog-validate", "validate a local catalog without a database or credentials"),
        ("catalog-locate", "locate a definition in the catalog source files"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--dir", help="catalog directory; defaults to nearest catalog.yaml")
        command.add_argument("--json", action="store_true", help="emit machine-readable output")
        if name == "catalog-locate":
            command.add_argument("reference", help="name or exact name@version")
            command.add_argument("--kind", help="definition family, e.g. artifacts or derivations")

    check = subparsers.add_parser(
        "catalog-check",
        help="report what publishing a catalog directory would do to a workspace",
    )
    check.add_argument("--workspace", required=True)
    check.add_argument("--dir", required=True, help="catalog directory to compile")
    check.add_argument("--package", help="defaults to the identity in catalog.yaml")

    graph = subparsers.add_parser(
        "catalog-graph",
        help="render one catalog package as an interactive dependency graph",
    )
    graph.add_argument("--dir", required=True, help="catalog directory to compile")
    graph.add_argument(
        "--package",
        help="exact name@semver package reference (default: the directory's only package)",
    )
    graph.add_argument(
        "--out",
        help="file to write; omit to write the page to stdout",
    )
    graph.add_argument(
        "--json",
        action="store_true",
        help="emit the graph as JSON instead of an interactive page",
    )

    prune = subparsers.add_parser(
        "catalog-prune",
        help="report which inactive definitions nothing references any more",
    )
    prune.add_argument("--workspace", required=True)

    contract = subparsers.add_parser(
        "migrate-collection-hashes",
        help="move stored records onto the record-contract identity",
    )
    contract.add_argument("--workspace", help="one workspace; omit to sweep every workspace")
    contract.add_argument("--dry-run", action="store_true", help="report without rewriting")

    backfill = subparsers.add_parser(
        "backfill",
        help="apply one processor to records that already exist (all of them by default)",
    )
    backfill.add_argument("--workspace", required=True)
    backfill.add_argument("--collection", required=True)
    backfill.add_argument("--version", type=int, required=True)
    backfill.add_argument("--processor", required=True)
    backfill.add_argument(
        "--max-rows",
        type=int,
        help=("optional ceiling on records scanned; omit to reach every eligible record"),
    )

    reembed = subparsers.add_parser(
        "reembed",
        help="embed existing records into another embedding space",
    )
    reembed.add_argument("--workspace", required=True)
    reembed.add_argument("--space", required=True, help="target embedding space id")
    reembed.add_argument("--max-rows", type=int, help="row budget for this pass")
    reembed.add_argument(
        "--cutover",
        action="store_true",
        help="promote the target space to active once coverage is complete",
    )

    rebind = subparsers.add_parser(
        "rebind-cursor",
        help="repoint a changes derivation cursor after a source-scope change",
    )
    rebind.add_argument("--workspace", required=True)
    rebind.add_argument("--derivation", required=True)
    rebind.add_argument("--entity", required=True)
    rebind.add_argument("--policy", choices=("reset", "carry"), required=True)

    mcp = subparsers.add_parser("mcp", help="serve the selected package's declared MCP interface")
    mcp.add_argument(
        "--url",
        "--base-url",
        dest="url",
        default=os.environ.get("MEMSEEK_URL"),
        help="Memseek HTTP API URL (default: MEMSEEK_URL)",
    )
    mcp.add_argument(
        "--api-key",
        default=os.environ.get("MEMSEEK_API_KEY"),
        help="Memseek workspace API key (default: MEMSEEK_API_KEY)",
    )
    mcp.add_argument(
        "--check",
        action="store_true",
        help="validate credentials and print the selected MCP interface without starting stdio",
    )

    evaluate = subparsers.add_parser("eval", help="run an evaluation against a running API")
    evaluations = evaluate.add_subparsers(dest="eval_command", required=True)
    learning = evaluations.add_parser(
        "skill-learning", help="compare scrape runs with and without learned skills"
    )
    learning.add_argument("--suite", type=Path, required=True)
    learning.add_argument("--trials", type=int, default=3)
    learning.add_argument(
        "--arms",
        default="cold,native,playbook,playbook+native",
        help="comma-separated: cold, native, playbook, playbook+native",
    )
    learning.add_argument("--k-train", type=int, default=3, help="training runs per trial")
    learning.add_argument(
        "--catalog",
        type=Path,
        default=Path("examples/site_scrape_catalog"),
        help="catalog directory published before the suite runs",
    )
    learning.add_argument("--package", default="site_scrape@1.0.0")
    learning.add_argument("--computer", default="scrape_workspace@1")
    learning.add_argument("--agent", default="site_scraper@1")
    learning.add_argument("--context-policy", default="scrape_budget@1")
    learning.add_argument("--url", default=os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000"))
    learning.add_argument("--api-key", default=os.environ.get("MEMSEEK_API_KEY"))
    learning.add_argument("--json", action="store_true", help="print the report as JSON")
    learning.add_argument(
        "--out",
        type=Path,
        help="results file (default: out/skill-learning/<suite>-<UTC time>.json)",
    )
    return parser


async def _run_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "migrate":
        revision = await apply_migrations(settings.database_url)
        print(json.dumps({"revision": revision}, separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "create-workspace":
        async with pool_lifespan(settings) as pool:
            credential = await create_workspace(pool, args.workspace)
        print(
            json.dumps(
                {"api_key": credential.api_key, "workspace": credential.workspace},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    if args.command == "worker":
        await run_worker(settings)
        return 0
    if args.command == "retry-job":
        job_id = UUID(args.job_id)
        async with pool_lifespan(settings) as pool:
            async with pool.connection() as conn:
                result = await conn.execute("select workspace from job where id = %s", (job_id,))
                row = await result.fetchone()
            if row is None:
                raise ValueError(f"job does not exist: {job_id}")
            from memseek.jobs import retry_dead_job

            status = await retry_dead_job(
                pool,
                workspace=str(row["workspace"]),
                job_id=job_id,
            )
        print(json.dumps(status, separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "reindex":
        from memseek.definitions import load_definition_catalog
        from memseek.reindex import reindex

        async with pool_lifespan(settings) as pool:
            result = await reindex(
                pool,
                workspace=args.workspace,
                settings=settings,
                catalog=load_definition_catalog(settings),
                since_seq=args.since_seq,
                reset=args.reset,
                confirm=args.yes,
            )
        print(json.dumps(result.as_json(), separators=(",", ":"), sort_keys=True))
        return 0
    if args.command in {"catalog-validate", "catalog-locate"}:
        from memseek.definitions import DefinitionError
        from memseek.definitions.manifest import (
            compile_catalog_files,
            discover_catalog,
            parse_manifest,
            read_catalog_files,
        )

        try:
            root = discover_catalog(Path(args.dir) if args.dir else None)
            files = read_catalog_files(root)
            manifest = parse_manifest(files["catalog.yaml"])
            catalog = compile_catalog_files(settings, files)
            if args.command == "catalog-validate":
                result = {"valid": True, "package": manifest.reference, "files": len(files)}
                print(
                    json.dumps(result)
                    if args.json
                    else f"Valid {manifest.reference} ({len(files)} files)"
                )
                return 0
            matches = [
                dict(item)
                for item in catalog.source_locations
                if (
                    item["reference"] == args.reference
                    or item["reference"].split("@", 1)[0] == args.reference
                )
                and (args.kind is None or item["kind"] == args.kind)
            ]
            # A derivation and its generated processor have one source location.
            if args.kind is None:
                matches = [
                    item
                    for item in matches
                    if item["kind"] != "processors"
                    or not any(
                        other["kind"] == "derivations" and other["reference"] == item["reference"]
                        for other in matches
                    )
                ]
            if not matches:
                raise DefinitionError("reference", f"definition {args.reference!r} not found")
            if len(matches) > 1:
                raise DefinitionError(
                    "ambiguous_reference",
                    "specify --kind and an exact version; matches: "
                    + ", ".join(f"{item['kind']} {item['reference']}" for item in matches),
                )
            item = matches[0]
            if args.json:
                print(json.dumps(item))
            else:
                print(
                    f"{root / item['file']}:{item['line']}:{item['column']}  {item['kind']} {item['reference']}"
                )
            return 0
        except DefinitionError as exc:
            if args.json:
                print(
                    json.dumps(
                        {
                            "valid": False,
                            "code": exc.code,
                            "message": exc.message,
                            "file": exc.file,
                            "path": exc.path,
                            "line": exc.line,
                            "column": exc.column,
                        }
                    )
                )
            else:
                print(str(exc), file=sys.stderr)
            return 1
    if args.command == "catalog-check":
        from memseek.definitions import load_definition_catalog
        from memseek.definitions.manifest import parse_manifest
        from memseek.sdk import _read_catalog_directory
        from memseek.workspace_catalog import (
            WorkspaceCatalogRegistry,
            WorkspaceCatalogRequest,
        )

        files = _read_catalog_directory(Path(args.dir))
        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            report, *_ = await registry.preflight(
                args.workspace,
                WorkspaceCatalogRequest(
                    package=args.package or parse_manifest(files["catalog.yaml"]).reference,
                    files=files,
                ),
            )
        print(json.dumps(report.as_json(), separators=(",", ":"), sort_keys=True))
        return 0 if report.publishable else 1
    if args.command == "catalog-graph":
        from memseek.catalog_graph import (
            build_package_graph,
            compile_catalog_directory,
            sole_package,
        )
        from memseek.catalog_graph_page import render_graph_page

        catalog = await asyncio.to_thread(
            compile_catalog_directory, Path(args.dir), settings=settings
        )
        graph = build_package_graph(catalog, args.package or sole_package(catalog))
        if args.json:
            output = json.dumps(graph.as_json(), separators=(",", ":"), sort_keys=True)
        else:
            output = render_graph_page(graph)
        if args.out is None:
            print(output)
        else:
            destination = Path(args.out)
            await asyncio.to_thread(destination.write_text, output, encoding="utf-8")
            print(
                json.dumps(
                    {
                        "package": graph.package,
                        "nodes": len(graph.nodes),
                        "edges": len(graph.edges),
                        "path": str(destination),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "catalog-prune":
        from memseek.definitions import load_definition_catalog
        from memseek.evolution import prune_definitions
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(args.workspace)
            report = await prune_definitions(pool, workspace=args.workspace, catalog=catalog)
        print(json.dumps(report.as_json(), separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "migrate-collection-hashes":
        from memseek.definitions import load_definition_catalog
        from memseek.evolution import migrate_collection_hashes, workspaces
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            targets = (args.workspace,) if args.workspace else await workspaces(pool)
            results = []
            for target in targets:
                catalog = await registry.get(target)
                result = await migrate_collection_hashes(
                    pool,
                    workspace=target,
                    catalog=catalog,
                    dry_run=args.dry_run,
                )
                results.append(result.as_json())
        print(json.dumps({"workspaces": results}, separators=(",", ":"), sort_keys=True))
        return 0 if all(item["complete"] for item in results) else 1
    if args.command == "backfill":
        from memseek.backfill import request_backfill
        from memseek.definitions import load_definition_catalog
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(args.workspace)
            handle = await request_backfill(
                pool,
                workspace=args.workspace,
                collection=args.collection,
                version=args.version,
                processor=args.processor,
                catalog=catalog,
                max_rows=args.max_rows,
            )
        print(json.dumps(handle.as_json(), separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "reembed":
        from memseek.definitions import load_definition_catalog
        from memseek.reembed import cutover_space, reembed
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(args.workspace)
            result = await reembed(
                pool,
                settings,
                catalog,
                workspace=args.workspace,
                space=args.space,
                max_rows=args.max_rows,
            )
            payload = result.as_json()
            if args.cutover:
                payload["cutover"] = (
                    await cutover_space(pool, workspace=args.workspace, space=args.space)
                ).as_json()
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "rebind-cursor":
        from memseek.definitions import load_definition_catalog
        from memseek.evolution import rebind_cursor
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(args.workspace)
            result = await rebind_cursor(
                pool,
                workspace=args.workspace,
                derivation=args.derivation,
                entity=args.entity,
                policy=args.policy,
                catalog=catalog,
                settings=settings,
            )
        print(json.dumps(result.as_json(), separators=(",", ":"), sort_keys=True))
        return 0
    if args.command == "mcp":
        if not args.url:
            raise ValueError("mcp requires --url or MEMSEEK_URL")
        if not args.api_key:
            raise ValueError("mcp requires --api-key or MEMSEEK_API_KEY")
        from memseek.mcp_server import inspect_mcp, run_stdio_mcp

        if args.check:
            result = await inspect_mcp(base_url=args.url, api_key=args.api_key)
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            return 0
        await run_stdio_mcp(base_url=args.url, api_key=args.api_key)
        return 0
    if args.command == "eval":
        return await _run_skill_learning_eval(args)
    raise AssertionError(f"unhandled command: {args.command}")


async def _run_skill_learning_eval(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from memseek.evals.skill_learning import (
        ApiBackend,
        RunRow,
        load_suite,
        parse_arms,
        planned_runs,
        render_runs,
        render_table,
        run_suite,
        summarize,
    )
    from memseek.sdk import MemseekClient

    if not args.api_key:
        raise ValueError("eval requires --api-key or MEMSEEK_API_KEY")
    arms = parse_arms(args.arms)
    if args.trials < 1 or args.k_train < 0:
        raise ValueError("--trials must be at least 1 and --k-train at least 0")
    suite = load_suite(args.suite)
    started = datetime.now(UTC)
    run = started.strftime("%Y%m%dT%H%M%SZ")
    out = args.out or Path("out/skill-learning") / f"{args.suite.stem}-{run}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    total = planned_runs(suite, arms=arms, trials=args.trials, k_train=args.k_train)
    rows: list[RunRow] = []

    def save(report: dict[str, object] | None) -> None:
        # Rewritten after every run, so a crash mid-suite keeps what finished.
        results = {
            "suite": str(args.suite),
            "started_at": started.isoformat(),
            "run": run,
            "arms": list(arms),
            "trials": args.trials,
            "k_train": args.k_train,
            "planned_runs": total,
            "runs": [row.content() for row in rows],
            "report": report,
        }
        out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def on_row(row: RunRow) -> None:
        rows.append(row)
        verdict = "pass" if row.passed else "fail"
        print(
            f"[{len(rows)}/{total}] {row.task} {row.arm} trial {row.trial} {row.phase} "
            f"k={row.k_train}: {verdict}, score {row.score:.2f}",
            file=sys.stderr,
        )
        print(f"metrics {json.dumps(dict(row.metrics))}", file=sys.stderr, flush=True)
        save(None)

    async with MemseekClient(args.url, args.api_key) as client:
        # The workspace may be new (quickstart's database lives in tmpfs), so
        # the eval installs the definitions it runs rather than assuming them.
        await client.catalog.publish(package=args.package, directory=args.catalog)
        backend = ApiBackend(
            client,
            computer=args.computer,
            agent=args.agent,
            context_policy=args.context_policy,
            results_entity=f"eval:{args.suite.stem}",
        )
        await run_suite(
            suite,
            backend,
            arms=arms,
            trials=args.trials,
            k_train=args.k_train,
            run=run,
            on_row=on_row,
        )
    report = summarize(rows)
    save(report)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"{render_runs(rows)}\n\n{render_table(report)}")
    print(f"results saved to {out}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and synchronously drive async command internals."""

    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(logging.DEBUG if settings.llm_debug else logging.INFO)
    try:
        return asyncio.run(_run_command(args, settings))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "detail": str(exc)},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
