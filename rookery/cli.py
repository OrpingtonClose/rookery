"""`rookery` CLI entry point.

Stage 1 exposes the commands that actually do something today:
    rookery doctor     — print config; verify model endpoint reachable
    rookery status     — show datalake state for a repo
    rookery tear-down  — run comprehension (scaffolded)

Stage 2/3 will add:
    rookery ask / explain / verify / history

Keep the CLI thin. Actual behavior lives in the modules.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console

from rookery.config import Config, ConfigError

console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )


async def _cmd_doctor(config: Config) -> int:
    import httpx

    console.print(f"[bold]model:[/bold]     {config.model}")
    console.print(f"[bold]base_url:[/bold]  {config.base_url}")
    console.print(f"[bold]datalake:[/bold]  {config.datalake_dir}")
    console.print(f"[bold]prehook:[/bold]   {config.prehook_budget_s:.1f}s")
    console.print(f"[bold]critic:[/bold]    {config.critic_budget_s:.1f}s")
    console.print(f"[bold]clone_cap:[/bold] {config.clone_cap_tokens} tokens")

    # Probe the model endpoint for reachability
    async with httpx.AsyncClient(timeout=10.0) as client:
        url = f"{config.base_url.rstrip('/')}/models"
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {config.api_key}"},
            )
        except httpx.RequestError as exc:
            console.print(f"[red]endpoint unreachable:[/red] {exc}")
            return 2

    if resp.status_code == 200:
        console.print(f"[green]endpoint ok:[/green] {url} → {resp.status_code}")
        return 0

    console.print(f"[yellow]endpoint responded {resp.status_code}:[/yellow] {url}")
    console.print(
        "Note: some providers do not expose /models. A non-200 here is not "
        "necessarily fatal — try a real consult call to confirm."
    )
    return 0


async def _cmd_status(config: Config, repo: str | None) -> int:
    from rookery.datalake.store import IndexDb

    db_path = config.datalake_dir / "index.duckdb"
    if not db_path.exists():
        console.print(f"[yellow]no datalake at {db_path}[/yellow]")
        return 0

    idx = IndexDb.open(db_path)
    try:
        total = idx.conn.execute("SELECT COUNT(*) FROM rows").fetchone()
        clones = idx.conn.execute(
            "SELECT id, MAX(version) FROM clones GROUP BY id ORDER BY id"
        ).fetchall()

        console.print(f"[bold]datalake:[/bold] {db_path}")
        console.print(f"[bold]rows:[/bold]     {total[0] if total else 0}")
        console.print("[bold]clones:[/bold]")
        if not clones:
            console.print("  (none materialized yet)")
        else:
            for clone_id, version in clones:
                console.print(f"  {clone_id}  v{version}")
    finally:
        idx.close()
    return 0


async def _cmd_tear_down(config: Config, repo_path: Path, repo_id: str | None) -> int:
    from rookery.tearing_down.pipeline import TearDownPipeline

    repo_path = repo_path.resolve()
    if not repo_path.exists():
        console.print(f"[red]path does not exist:[/red] {repo_path}")
        return 2

    pipe = TearDownPipeline(
        config=config,
        repo_path=repo_path,
        repo_id=repo_id,
    )
    result = await pipe.run()

    console.rule(f"tear-down {result.run_id}")
    console.print(f"[bold]repo:[/bold]     {result.repo_id}")
    console.print(f"[bold]angles:[/bold]   {len(result.angles_detected)} detected")
    for a in result.angles_detected:
        console.print(f"  • {a}")

    console.print(f"[bold]clones:[/bold]   {len(result.clones_materialized)} materialized")
    for ref in result.clones_materialized:
        console.print(
            f"  • {ref.clone_id:24s} v{ref.version}  prefix_sha={ref.prefix_sha256[:12]}…"
        )

    if result.worker_failures:
        console.print(f"[yellow]failures:[/yellow] {len(result.worker_failures)}")
        for cid, err in result.worker_failures:
            console.print(f"  [red]✗[/red] {cid}: {err[:140]}")

    u = result.total_llm_usage
    if u:
        console.print(
            f"[dim]tokens: prompt={u.get('prompt_tokens', 0)} "
            f"completion={u.get('completion_tokens', 0)} "
            f"reasoning={u.get('reasoning_tokens', 0)} "
            f"cache_hit={u.get('cache_hit_tokens', 0)}"
        )

    if result.finished_at:
        dt = (result.finished_at - result.started_at).total_seconds()
        console.print(f"[dim]elapsed: {dt:.1f}s")

    return 0 if result.clones_materialized else 1


async def _cmd_ask(
    config: Config,
    repo_id: str,
    clone_id: str,
    question: str,
    *,
    max_tokens: int = 6000,
) -> int:
    from rookery.operator.ask import ask_clone

    try:
        result = await ask_clone(
            config=config,
            repo_id=repo_id,
            clone_id=clone_id,
            question=question,
            max_tokens=max_tokens,
        )
    except LookupError as exc:
        console.print(f"[red]clone not found:[/red] {exc}")
        return 2

    console.rule(f"{result.clone_id} v{result.clone_version}")
    if result.answer:
        console.print(result.answer)
    else:
        console.print("[yellow](empty answer)[/yellow]")
        if result.reasoning:
            console.print(
                "[dim]reasoning consumed the token budget — "
                "retry with --max-tokens larger than "
                f"{result.completion_tokens}[/dim]"
            )
    console.print()
    console.print(
        f"[dim]tokens: prompt={result.prompt_tokens} "
        f"completion={result.completion_tokens} "
        f"cache_hit={result.cache_hit_tokens} "
        f"elapsed={result.elapsed_s:.1f}s"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rookery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="verify config and model endpoint")

    p_status = sub.add_parser("status", help="show datalake state")
    p_status.add_argument("--repo", default=None, help="repo id filter (optional)")

    p_tear = sub.add_parser("tear-down", help="run comprehension pipeline")
    p_tear.add_argument("path", type=Path, help="repository root")
    p_tear.add_argument(
        "--repo-id", default=None, help="logical repo id (defaults to path basename)"
    )

    p_ask = sub.add_parser("ask", help="ask a clone a question")
    p_ask.add_argument("--repo-id", required=True)
    p_ask.add_argument(
        "--max-tokens",
        type=int,
        default=6000,
        help="max completion tokens (reasoning counts against this)",
    )
    p_ask.add_argument("clone", help="clone id, e.g. invariant_keeper")
    p_ask.add_argument("question", help="the question")

    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        return 2

    _setup_logging(config.log_level)

    if args.cmd == "doctor":
        return asyncio.run(_cmd_doctor(config))
    if args.cmd == "status":
        return asyncio.run(_cmd_status(config, args.repo))
    if args.cmd == "tear-down":
        return asyncio.run(_cmd_tear_down(config, args.path, args.repo_id))
    if args.cmd == "ask":
        return asyncio.run(
            _cmd_ask(
                config,
                args.repo_id,
                args.clone,
                args.question,
                max_tokens=args.max_tokens,
            )
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
