"""Command-line entry point for agent-session-exporter."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from . import __version__
from .claude_cloud import pull_events
from .codex_cloud import (
    exec_codex_cloud,
    sync_codex_cloud,
)
from .core import (
    DEFAULT_DESTINATION,
    EventStore,
    default_config_path,
    load_config,
    normalize_event,
    read_stdin_json,
    render_initial_config,
)
from .hooks import (
    claude_cloud_hooks,
    claude_hooks,
    codex_hooks,
    install_local_hooks,
)
from .importers import import_export
from .renderer import (
    migrate_render_state,
    render_state_path_issues,
    sync_session,
    sync_vault,
)


def _config_path(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_config_path()


def _valid_destination(value: str) -> str:
    normalized = value.strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError(
            "destination must be a relative Vault path without `..`."
        )
    return path.as_posix()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-session-exporter",
        description="Archive Codex and Claude sessions into an Obsidian Vault.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="configuration file (default: XDG config directory)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a configuration file")
    init.add_argument("--vault", type=Path, required=True)
    init.add_argument(
        "--destination",
        type=_valid_destination,
        default=DEFAULT_DESTINATION,
    )
    init.add_argument("--force", action="store_true")

    capture = subparsers.add_parser(
        "capture",
        help="read one hook JSON object from stdin",
    )
    capture.add_argument("--source", required=True)
    capture.add_argument("--verbose", action="store_true")

    subparsers.add_parser("sync", help="render changed sessions to the Vault")

    migrate = subparsers.add_parser(
        "migrate-destination",
        help="move generated notes and repair render paths",
    )
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="apply the displayed plan (default: dry-run)",
    )

    pull = subparsers.add_parser(
        "pull",
        help="pull Claude Cloud events from the Worker inbox",
    )
    pull.add_argument("--limit", type=int, default=500)
    pull.add_argument("--sync", action="store_true")

    cloud_sync = subparsers.add_parser(
        "codex-cloud-sync",
        help="ingest tasks exposed by the Codex Cloud CLI",
    )
    cloud_sync.add_argument("--limit", type=int, default=20)
    cloud_sync.add_argument("--details", action="store_true")
    cloud_sync.add_argument("--sync", action="store_true")

    cloud_exec = subparsers.add_parser(
        "codex-cloud-exec",
        help="submit a Codex Cloud task and retain its initial prompt",
    )
    cloud_exec.add_argument("--env", required=True)
    cloud_exec.add_argument("--branch", default="")
    cloud_exec.add_argument("query")

    importer = subparsers.add_parser(
        "import-export",
        help="import a ChatGPT or Claude data-export ZIP/JSON",
    )
    importer.add_argument("path", type=Path)
    importer.add_argument(
        "--source",
        choices=["auto", "chatgpt", "claude"],
        default="auto",
    )
    importer.add_argument("--sync", action="store_true")

    hooks = subparsers.add_parser(
        "hooks",
        help="print a hook configuration snippet",
    )
    hooks.add_argument(
        "--source",
        choices=["codex", "claude", "claude-cloud"],
        required=True,
    )
    hooks.add_argument(
        "--executable",
        default="ase",
        help="command name or absolute executable path for command hooks",
    )
    hooks.add_argument(
        "--inbox-url",
        "--collector-url",
        dest="inbox_url",
        default="",
        help="public HTTPS URL of the Claude Cloud Worker inbox",
    )

    install_hooks = subparsers.add_parser(
        "install-hooks",
        help="merge command hooks into a user-level settings file",
    )
    install_hooks.add_argument(
        "--source",
        choices=["codex", "claude"],
        required=True,
    )
    install_hooks.add_argument(
        "--executable",
        default="ase",
        help="command name or absolute executable path used by hooks",
    )
    install_hooks.add_argument(
        "--target",
        type=Path,
        help="override ~/.codex/hooks.json or ~/.claude/settings.json",
    )

    subparsers.add_parser("doctor", help="check local configuration")
    return parser


def _init(args: argparse.Namespace, config_path: Path) -> int:
    if config_path.exists() and not args.force:
        raise ValueError(
            f"Configuration already exists: {config_path} (use --force to replace)"
        )
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        config_path.parent.chmod(0o700)
    except OSError:
        pass
    content = render_initial_config(args.vault, args.destination)
    descriptor = os.open(
        config_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    print(f"Created {config_path}")
    print("Next: add hooks with `ase hooks --source codex` or `--source claude`.")
    return 0


def _capture(args: argparse.Namespace, config_path: Path) -> int:
    config = load_config(config_path)
    payload = read_stdin_json()
    envelope = normalize_event(payload, args.source, config)
    with EventStore(config.state_dir) as store:
        event_id, inserted = store.add_event(envelope)
    if config.sync_on_capture and config.vault_path is not None:
        try:
            sync_session(
                config,
                str(envelope["source"]),
                str(envelope["device_id"]),
                str(envelope["session_id"]),
            )
        except (OSError, TypeError, ValueError) as error:
            print(f"warning: Vault sync failed: {error}", file=sys.stderr)
    if args.verbose:
        print(
            f"event={event_id} inserted={str(inserted).lower()}",
            file=sys.stderr,
        )
    return 0


def _doctor(config_path: Path) -> int:
    config = load_config(config_path)
    failures = 0
    checks: list[tuple[str, bool, str]] = []
    checks.append(
        (
            "config",
            config_path.exists(),
            str(config_path),
        )
    )
    checks.append(
        (
            "vault",
            config.vault_path is not None and config.vault_path.exists(),
            str(config.vault_path or "not configured"),
        )
    )
    try:
        with EventStore(config.state_dir):
            state_ok = True
    except OSError:
        state_ok = False
    checks.append(("state", state_ok, str(config.state_dir)))
    if state_ok and config.vault_path is not None and config.vault_path.exists():
        render_issues = render_state_path_issues(config)
        checks.append(
            (
                "render paths",
                not render_issues,
                "ok" if not render_issues else "; ".join(render_issues[:3]),
            )
        )
    checks.append(
        (
            "codex CLI",
            shutil.which("codex") is not None,
            shutil.which("codex") or "not found (only needed for Codex Cloud)",
        )
    )
    if config.claude_cloud.url:
        token = os.environ.get(config.claude_cloud.token_env, "")
        checks.append(
            (
                "Claude Cloud pull token",
                bool(token),
                config.claude_cloud.token_env,
            )
        )
    for label, passed, detail in checks:
        mark = "ok" if passed else "FAIL"
        print(f"[{mark:4}] {label}: {detail}")
        if not passed and label not in {"codex CLI"}:
            failures += 1
    return 1 if failures else 0


def run(arguments: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(arguments)
    config_path = _config_path(args.config)

    if args.command == "init":
        return _init(args, config_path)
    if args.command == "hooks":
        if args.source == "codex":
            print(codex_hooks(args.executable))
        elif args.source == "claude":
            print(claude_hooks(args.executable))
        else:
            if not args.inbox_url:
                raise ValueError("--inbox-url is required for claude-cloud hooks.")
            print(claude_cloud_hooks(args.inbox_url))
        return 0
    if args.command == "install-hooks":
        target, added = install_local_hooks(
            args.source,
            executable=args.executable,
            target=args.target,
        )
        print(f"Updated {target} (added {added} hook groups)")
        if args.source == "codex":
            print("Open `/hooks` in Codex to review and trust the new hooks.")
        return 0
    if args.command == "doctor":
        return _doctor(config_path)
    if args.command == "capture":
        return _capture(args, config_path)

    config = load_config(config_path)
    if args.command == "sync":
        written, unchanged = sync_vault(config)
        print(f"written={written} unchanged={unchanged}")
        return 0
    if args.command == "migrate-destination":
        migrations, errors = migrate_render_state(config, apply=args.apply)
        prefix = "apply" if args.apply else "dry-run"
        for migration in migrations:
            backup = (
                f" backup={migration.backup_path}"
                if migration.backup_path is not None
                else ""
            )
            print(
                f"{prefix}: {migration.operation} "
                f"{migration.old_path} -> {migration.new_path}{backup}"
            )
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        print(
            f"planned={len(migrations)} applied="
            f"{len(migrations) if args.apply else 0} errors={len(errors)}"
        )
        return 1 if errors else 0
    if args.command == "pull":
        imported, cursor = pull_events(config, limit=args.limit)
        print(f"imported={imported} cursor={cursor}")
        if args.sync:
            written, unchanged = sync_vault(config)
            print(f"written={written} unchanged={unchanged}")
        return 0
    if args.command == "codex-cloud-sync":
        inserted = sync_codex_cloud(
            config,
            limit=args.limit,
            include_details=args.details,
        )
        print(f"inserted={inserted}")
        if args.sync:
            written, unchanged = sync_vault(config)
            print(f"written={written} unchanged={unchanged}")
        return 0
    if args.command == "codex-cloud-exec":
        output = exec_codex_cloud(
            config,
            args.query,
            environment=args.env,
            branch=args.branch,
        )
        print(output, end="" if output.endswith("\n") else "\n")
        return 0
    if args.command == "import-export":
        inserted, examined = import_export(
            args.path.expanduser(),
            config,
            source=args.source,
        )
        print(f"inserted={inserted} examined={examined}")
        if args.sync:
            written, unchanged = sync_vault(config)
            print(f"written={written} unchanged={unchanged}")
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


def main() -> None:
    """Console script wrapper with concise user-facing errors."""
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
