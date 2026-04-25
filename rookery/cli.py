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


async def _cmd_tear_down(config: Config, repo_path: Path) -> int:
    from rookery.tearing_down.pipeline import TearDownPipeline

    pipe = TearDownPipeline(config=config, repo_path=repo_path)
    result = await pipe.run()
    console.print(f"[bold]run_id:[/bold] {result.run_id}")
    console.print(f"[bold]angles:[/bold] {', '.join(result.angles_detected)}")
    for note in result.notes:
        console.print(f"  • {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rookery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="verify config and model endpoint")

    p_status = sub.add_parser("status", help="show datalake state")
    p_status.add_argument("--repo", default=None, help="repo id filter (optional)")

    p_tear = sub.add_parser("tear-down", help="run comprehension pipeline")
    p_tear.add_argument("path", type=Path, help="repository root")

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
        return asyncio.run(_cmd_tear_down(config, args.path))

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
