"""``docs/cli.md``: every command and option of the ``dvf`` command line, from the Typer app."""

from __future__ import annotations

import enum
import inspect
from pathlib import PurePath
from typing import Any

import typer

from dvf.docsgen.markdown import GENERATED_NOTE, code, literal, slug, table


def _clean_help(text: str | None) -> str:
    if not text:
        return ""
    lines = [line for line in inspect.cleandoc(text).splitlines() if line.strip() != "\b"]
    return "\n".join(lines).replace("``", "`").strip()


def _first_line(text: str | None) -> str:
    cleaned = _clean_help(text)
    return " ".join(cleaned.split("\n\n", 1)[0].split())


# Typer bundles its own copy of Click, so parameters and commands are recognized by their
# attributes rather than by Click's classes.


def _is_option(param: Any) -> bool:
    return bool(getattr(param, "param_type_name", "") == "option")


def _is_group(command: Any) -> bool:
    return hasattr(command, "list_commands")


def _context(command: Any, name: str) -> Any:
    return command.context_class(command, info_name=name)


def _type_text(param: Any) -> str:
    ptype = param.type
    kind = type(ptype).__name__
    if _is_option(param) and param.is_flag:
        return "flag"
    if hasattr(ptype, "choices"):
        text = "one of " + ", ".join(code(str(getattr(c, "value", c))) for c in ptype.choices)
    elif kind in ("IntRange", "FloatRange"):
        base = "integer" if kind == "IntRange" else "number"
        limits = []
        if ptype.min is not None:
            limits.append(f"{'>' if ptype.min_open else '>='} {ptype.min}")
        if ptype.max is not None:
            limits.append(f"{'<' if ptype.max_open else '<='} {ptype.max}")
        text = base + (f" ({', '.join(limits)})" if limits else "")
    elif kind == "Path":
        text = "path"
    else:
        names = {"float": "number", "str": "text", "string": "text", "int": "integer"}
        text = names.get(ptype.name, ptype.name)
    if param.multiple:
        text += ", repeatable"
    return text


def _default_text(param: Any) -> str:
    if param.required:
        return "**required**"
    default = param.default
    if callable(default) or default is None or default == () or default == []:
        return ""
    if _is_option(param) and param.is_flag:
        if param.secondary_opts:
            return code(param.opts[0] if default else param.secondary_opts[0])
        return "" if not default else "on"
    if isinstance(default, enum.Enum):
        default = default.value
    if isinstance(default, PurePath):
        return code(default.as_posix())
    if isinstance(default, list | tuple):
        return ", ".join(literal(v) for v in default)
    return literal(default)


def _names(param: Any) -> str:
    if not _is_option(param):
        return code(param.human_readable_name.upper())
    names = ", ".join(code(o) for o in param.opts)
    if param.secondary_opts:
        names += " / " + ", ".join(code(o) for o in param.secondary_opts)
    return names


def _param_rows(command: Any) -> list[list[str]]:
    rows = []
    for param in command.params:
        if getattr(param, "hidden", False) or param.name == "help":
            continue
        help_text = " ".join(_clean_help(getattr(param, "help", None)).split())
        envvar = getattr(param, "envvar", None)
        if envvar:
            help_text += f" Environment variable: {code(str(envvar))}."
        rows.append([_names(param), _type_text(param), _default_text(param), help_text.strip()])
    return rows


def _usage(command: Any, path: list[str]) -> str:
    pieces = [*path, "[OPTIONS]"]
    for param in command.params:
        if _is_option(param) or getattr(param, "hidden", False):
            continue
        name = param.human_readable_name.upper()
        if getattr(param, "nargs", 1) == -1:
            name += "..."
        pieces.append(name if param.required else f"[{name}]")
    if _is_group(command):
        pieces.append("COMMAND [ARGS]...")
    return " ".join(pieces)


def _walk(command: Any, path: list[str], out: list[tuple[list[str], Any]]) -> None:
    out.append((path, command))
    if _is_group(command):
        ctx = _context(command, path[-1])
        for name in sorted(command.list_commands(ctx)):
            sub = command.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            _walk(sub, [*path, name], out)


def cli_page(app: typer.Typer | None = None) -> str:
    """Render every command of ``app`` (default: the ``dvf`` application)."""
    if app is None:
        from dvf.cli.app import app as dvf_app

        app = dvf_app
    root = typer.main.get_command(app)
    commands: list[tuple[list[str], Any]] = []
    _walk(root, ["dvf"], commands)

    out = [
        "# Command line\n",
        GENERATED_NOTE.format(source="the `dvf` command line application"),
        "Every command and option. `dvf COMMAND --help` prints the same information. Options "
        "that take a value accept `--option value` or `--option=value`.\n",
        "## Exit codes\n",
        table(
            ["Code", "Meaning"],
            [
                ["`0`", "Passed (warnings allowed unless `--strict`)."],
                [
                    "`1`",
                    "Validation failures at or above `--fail-on`, a quality score below "
                    "`--min-score`, or a `--check` that found work to do.",
                ],
                ["`2`", "Execution error: a connection, query, or engine failed."],
                ["`3`", "Configuration or usage error."],
            ],
        ),
        "## Commands\n",
        table(
            ["Command", "Purpose"],
            [
                [f"[{code(' '.join(p))}](#{slug(' '.join(p))})", _first_line(c.help)]
                for p, c in commands[1:]
                if not _is_group(c)
            ],
        ),
    ]
    for path, command in commands:
        name = " ".join(path)
        level = "##" if len(path) <= 2 else "###"
        out.append(f"{level} {code(name)} {{#{slug(name)}}}\n")
        help_text = _clean_help(command.help)
        if path == ["dvf"]:
            help_text = (
                help_text + "\n\nGlobal options go before the command: "
                "`dvf --plugins ./plugins schema config`."
            )
        if help_text:
            out.append(help_text + "\n")
        if _is_group(command) and path != ["dvf"]:
            ctx = _context(command, name)
            subs = ", ".join(
                f"[{code(f'{name} {s}')}](#{slug(f'{name} {s}')})"
                for s in sorted(command.list_commands(ctx))
                if (sub := command.get_command(ctx, s)) is not None and not sub.hidden
            )
            out.append(f"Subcommands: {subs}.\n")
        out.append(f"```text\n{_usage(command, path)}\n```\n")
        rows = _param_rows(command)
        if rows:
            out.append(table(["Argument or option", "Type", "Default", "Description"], rows))
    text = "\n".join(out)
    lines: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line and lines and not lines[-1]:
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"
