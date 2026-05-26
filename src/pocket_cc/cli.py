"""Top-level CLI entry point for pocket-cc."""

from __future__ import annotations

import logging
import sys

import click

from pocket_cc import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pocket-cc")
def main() -> None:
    """pocket-cc — drive remote Claude Code from Lark."""


@main.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable DEBUG-level logging (otherwise INFO).",
)
def run(verbose: bool) -> None:
    """Run the pocket-cc bot — connect to Lark, manage tmux, stream Claude output."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from pocket_cc.app.bootstrap import Pocketcc
    from pocket_cc.app.config import ConfigError
    from pocket_cc.app.config import load as load_config

    try:
        cfg = load_config()
    except ConfigError as e:
        click.echo(f"❌ {e}", err=True)
        sys.exit(2)

    click.echo("pocket-cc starting…")
    click.echo(f"  tmux session  : {cfg.tmux_session}")
    click.echo(f"  claude command: {cfg.claude_command}")
    click.echo(f"  users         : {len(cfg.users)}")
    for user in cfg.users.values():
        click.echo(f"    - {user.display_name} ({user.open_id}) → {user.workspace}")
    click.echo(f"  patch interval: {cfg.patch_interval_s}s")
    click.echo(f"  poll interval : {cfg.transcript_poll_s}s")
    click.echo()

    app = Pocketcc(cfg)
    try:
        app.start()
    except KeyboardInterrupt:
        click.echo("\npocket-cc stopped")


# ============================================================== hook subcommand


@main.group()
def hook() -> None:
    """Manage Claude Code hook registration."""


@hook.command("install")
def hook_install() -> None:
    """Register pocket-cc hooks in ~/.claude/settings.json (idempotent)."""
    from pocket_cc.claude.hooks import (
        claude_settings_path,
        hook_command_string,
        install_hooks,
    )

    path = claude_settings_path()
    result = install_hooks()
    click.echo(f"settings: {path}")
    click.echo(f"command : {hook_command_string()} <Event>")
    click.echo()
    for event, changed in result.items():
        flag = "✓ updated" if changed else "= unchanged"
        click.echo(f"  {flag}  {event}")
    click.echo()
    click.echo("✅ pocket-cc hooks installed.")


@hook.command("uninstall")
def hook_uninstall() -> None:
    """Remove pocket-cc hooks from ~/.claude/settings.json."""
    from pocket_cc.claude.hooks import claude_settings_path, uninstall_hooks

    path = claude_settings_path()
    result = uninstall_hooks()
    click.echo(f"settings: {path}")
    click.echo()
    for event, removed in result.items():
        flag = "✓ removed " if removed else "  (none)  "
        click.echo(f"  {flag}  {event}")
    click.echo()
    click.echo("✅ pocket-cc hooks uninstalled.")


@hook.command("status")
def hook_status_cmd() -> None:
    """Show which Claude hook events have pocket-cc registered."""
    from pocket_cc.claude.hooks import claude_settings_path, hook_status

    path = claude_settings_path()
    click.echo(f"settings: {path}")
    click.echo()
    for s in hook_status():
        marker = "✓" if s.installed else "✗"
        extras = f"  (+{s.other_entries} other entries)" if s.other_entries else ""
        click.echo(f"  {marker}  {s.event}{extras}")


@hook.command("receive", hidden=True)
@click.argument("event_name")
def hook_receive(event_name: str) -> None:
    """Internal: Claude Code invokes this; reads stdin payload, writes events.jsonl."""
    from pocket_cc.claude.hooks import receive_event

    sys.exit(receive_event(event_name))


if __name__ == "__main__":
    main()
