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
    check = subparsers.add_parser(
        "catalog-check",
        help="report what publishing a catalog directory would do to a workspace",
    )
    check.add_argument("--workspace", required=True)
    check.add_argument("--dir", required=True, help="catalog directory to compile")
    check.add_argument("--package", required=True, help="exact name@semver package reference")

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
    if args.command == "catalog-check":
        from memseek.definitions import load_definition_catalog
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
                WorkspaceCatalogRequest(package=args.package, files=files),
            )
        print(json.dumps(report.as_json(), separators=(",", ":"), sort_keys=True))
        return 0 if report.publishable else 1
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
    raise AssertionError(f"unhandled command: {args.command}")


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
