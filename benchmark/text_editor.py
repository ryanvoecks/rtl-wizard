"""Custom `text_editor` tool that supersedes the one shipped with inspect_ai.

Differences from upstream:
  * `create` overwrites existing files (instead of erroring), and the return
    message announces the overwrite so the model knows it happened.
  * `delete` removes a file (records prior contents so `undo_edit` can restore).
  * `replace_lines` swaps a 1-indexed inclusive line range for a new string.
  * `replace_str` replaces every occurrence of a substring and reports the
    count (replaces upstream's `str_replace`, which is no longer exposed).

Implementation runs entirely host-side against `inspect_ai.util.sandbox()`, so it
needs no companion changes in the sandbox image.
"""

import json

from inspect_ai.tool import Tool, ToolError, ToolResult, tool
from inspect_ai.util import sandbox

SNIPPET_LINES = 4
MAX_RESPONSE_LEN = 16000
TRUNCATED_NOTE = (
    "<response clipped><NOTE>To save on context only part of this file has "
    "been shown to you. You should retry this tool after you have searched "
    "inside the file with `grep -n` in order to find the line numbers of "
    "what you are looking for.</NOTE>"
)
HISTORY_PATH = "/tmp/inspect_editor_history.json"


def _truncate(content: str) -> str:
    if len(content) <= MAX_RESPONSE_LEN:
        return content
    return content[:MAX_RESPONSE_LEN] + TRUNCATED_NOTE


def _format(content: str, descriptor: str, init_line: int = 1) -> str:
    content = _truncate(content).expandtabs()
    numbered = "\n".join(
        f"{i + init_line:6}\t{line}" for i, line in enumerate(content.split("\n"))
    )
    return f"Here's the result of running `cat -n` on {descriptor}:\n{numbered}\n"


def _validate_abs(path: str) -> None:
    if not path.startswith("/"):
        raise ToolError(
            f"The path {path} is not an absolute path; it must start with `/`."
        )


async def _exists(path: str) -> bool:
    return (await sandbox().exec(["test", "-e", path])).returncode == 0


async def _is_dir(path: str) -> bool:
    return (await sandbox().exec(["test", "-d", path])).returncode == 0


async def _read(path: str) -> str:
    try:
        return await sandbox().read_file(path)
    except FileNotFoundError:
        raise ToolError(f"The path {path} does not exist.") from None
    except IsADirectoryError:
        raise ToolError(f"The path {path} is a directory.") from None


async def _load_history() -> dict[str, list]:
    try:
        raw = await sandbox().read_file(HISTORY_PATH)
    except FileNotFoundError:
        return {}
    return json.loads(raw)


async def _save_history(history: dict[str, list]) -> None:
    await sandbox().write_file(HISTORY_PATH, json.dumps(history))


async def _push_history(path: str, entry) -> None:
    history = await _load_history()
    history.setdefault(path, []).append(entry)
    await _save_history(history)


async def _view(path: str, view_range: list[int] | None) -> str:
    if not await _exists(path):
        raise ToolError(f"The path {path} does not exist.")
    if await _is_dir(path):
        result = await sandbox().exec(
            ["find", path, "-maxdepth", "2", "-not", "-path", "*/.*"]
        )
        if result.returncode != 0:
            raise ToolError(f"Error listing {path}: {result.stderr}")
        listing = result.stdout.strip()
        return (
            f"Here are the files and directories up to 2 levels deep in {path}, "
            f"excluding hidden items:\n{listing}\n"
        )

    content = await _read(path)
    init_line = 1
    if view_range is not None:
        if len(view_range) != 2 or not all(isinstance(i, int) for i in view_range):
            raise ToolError("Invalid `view_range`. Provide a list of two integers.")
        lines = content.split("\n")
        n = len(lines)
        init_line, final_line = view_range
        if init_line < 1 or init_line > n:
            raise ToolError(
                f"Invalid `view_range`: {view_range}. First element {init_line} "
                f"out of file range [1, {n}]."
            )
        if final_line > n:
            final_line = -1
        if final_line != -1 and final_line < init_line:
            raise ToolError(
                f"Invalid `view_range`: {view_range}. End {final_line} < start {init_line}."
            )
        content = (
            "\n".join(lines[init_line - 1 :])
            if final_line == -1
            else "\n".join(lines[init_line - 1 : final_line])
        )
    return _format(content, path, init_line=init_line)


async def _create(path: str, file_text: str) -> str:
    existed = await _exists(path)
    if existed and await _is_dir(path):
        raise ToolError(
            f"The path {path} is a directory; refusing to overwrite with `create`."
        )
    prior = await _read(path) if existed else None
    await sandbox().write_file(path, file_text)
    await _push_history(path, prior if prior is not None else -1)
    if existed:
        return (
            f"File at {path} already existed; its previous contents were "
            "OVERWRITTEN with the new file_text. Use `undo_edit` if this "
            "was unintended."
        )
    return f"File created successfully at: {path}"


async def _delete(path: str) -> str:
    if not await _exists(path):
        raise ToolError(f"The path {path} does not exist.")
    if await _is_dir(path):
        raise ToolError(f"The path {path} is a directory; only files can be deleted.")
    prior = await _read(path)
    result = await sandbox().exec(["rm", "-f", path])
    if result.returncode != 0:
        raise ToolError(f"Failed to delete {path}: {result.stderr}")
    await _push_history(path, prior)
    return f"File deleted at: {path}"


async def _replace_str(
    path: str, old_str: str | None, new_str: str | None
) -> str:
    if not old_str:
        raise ToolError("`old_str` is required and must be non-empty.")
    if not await _exists(path):
        raise ToolError(f"The path {path} does not exist.")
    content = (await _read(path)).expandtabs()
    old_e = old_str.expandtabs()
    new_e = (new_str or "").expandtabs()

    occurrences = content.count(old_e)
    if occurrences == 0:
        raise ToolError(
            f"No replacement performed: `{old_str}` did not appear in {path}."
        )

    new_content = content.replace(old_e, new_e)
    await sandbox().write_file(path, new_content)
    await _push_history(path, content)

    first_line = content.split(old_e)[0].count("\n")
    snippet_start = max(0, first_line - SNIPPET_LINES)
    snippet_end = first_line + SNIPPET_LINES + new_e.count("\n")
    snippet = "\n".join(new_content.split("\n")[snippet_start : snippet_end + 1])
    plural = "s" if occurrences != 1 else ""
    return (
        f"The file {path} has been edited; replaced {occurrences} "
        f"occurrence{plural} of `old_str`. "
        + _format(snippet, f"a snippet of {path}", snippet_start + 1)
        + "Review the changes and make sure they are as expected. "
        "Edit the file again if necessary."
    )


async def _replace_lines(
    path: str, start_line: int, end_line: int, new_str: str
) -> str:
    if not await _exists(path):
        raise ToolError(f"The path {path} does not exist.")
    content = (await _read(path)).expandtabs()
    lines = content.split("\n")
    n = len(lines)
    if start_line < 1 or start_line > n:
        raise ToolError(
            f"Invalid `start_line`: {start_line}. File has {n} lines (valid range [1, {n}])."
        )
    if end_line < start_line or end_line > n:
        raise ToolError(
            f"Invalid `end_line`: {end_line}. Must be in [{start_line}, {n}]."
        )

    new_lines = new_str.expandtabs().split("\n")
    rewritten = lines[: start_line - 1] + new_lines + lines[end_line:]
    new_content = "\n".join(rewritten)
    await sandbox().write_file(path, new_content)
    await _push_history(path, content)

    snippet_start = max(0, start_line - 1 - SNIPPET_LINES)
    snippet_end = start_line - 1 + len(new_lines) + SNIPPET_LINES
    snippet = "\n".join(rewritten[snippet_start:snippet_end])
    return (
        f"The file {path} has been edited "
        f"(lines {start_line}-{end_line} -> {len(new_lines)} line(s)). "
        + _format(snippet, f"a snippet of {path}", snippet_start + 1)
        + "Review the changes and make sure they are as expected. "
        "Edit the file again if necessary."
    )


async def _insert(path: str, insert_line: int, new_str: str) -> str:
    if not await _exists(path):
        raise ToolError(f"The path {path} does not exist.")
    content = (await _read(path)).expandtabs()
    lines = content.split("\n")
    n = len(lines)
    if insert_line < 0 or insert_line > n:
        raise ToolError(
            f"Invalid `insert_line`: {insert_line}. File has {n} lines (valid range [0, {n}])."
        )

    new_lines = new_str.expandtabs().split("\n")
    rewritten = lines[:insert_line] + new_lines + lines[insert_line:]
    new_content = "\n".join(rewritten)
    await sandbox().write_file(path, new_content)
    await _push_history(path, content)

    snippet_start = max(0, insert_line - SNIPPET_LINES)
    snippet_end = insert_line + len(new_lines) + SNIPPET_LINES
    snippet = "\n".join(rewritten[snippet_start:snippet_end])
    return (
        f"The file {path} has been edited. "
        + _format(snippet, "a snippet of the edited file", snippet_start + 1)
        + "Review the changes and make sure they are as expected "
        "(correct indentation, no duplicate lines, etc). Edit the file again if necessary."
    )


async def _undo_edit(path: str) -> str:
    history = await _load_history()
    entries = history.get(path) or []
    if not entries:
        raise ToolError(f"No edit history found for {path}.")
    entry = entries.pop()
    history[path] = entries
    await _save_history(history)

    if entry == -1:
        if await _exists(path):
            await sandbox().exec(["rm", "-f", path])
        return f"File deleted at: {path}"
    await sandbox().write_file(path, entry)
    return f"Last edit to {path} undone successfully. {_format(entry, path)}"


@tool()
def text_editor(timeout: int | None = None) -> Tool:
    """Custom editing tool for viewing, creating, and editing files in the sandbox.

    Adds `delete`, `replace_lines`, and `replace_str` on top of the upstream
    commands and drops `str_replace`. `create` overwrites existing files but
    flags the overwrite in its return message; `replace_str` replaces every
    occurrence and reports how many were replaced.

    Args:
      timeout: Reserved for compatibility; the host-side implementation does
        not currently honor a timeout (sandbox calls have their own).
    """
    del timeout  # unused; kept so call sites remain compatible

    async def execute(
        command: str,
        path: str,
        file_text: str | None = None,
        view_range: list[int] | None = None,
        old_str: str | None = None,
        new_str: str | None = None,
        insert_line: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        """Execute a text-editor command against a file in the sandbox.

        Args:
          command: One of `view`, `create`, `delete`, `insert`, `replace_str`,
            `replace_lines`, `undo_edit`.
          path: Absolute path inside the sandbox (e.g. `/workspace/foo.v`).
          file_text: Required for `create`; full contents to write. If a file
            already exists at `path`, it is overwritten and the tool's return
            message will say so.
          view_range: Optional 1-indexed `[start, end]` range for `view` (use
            -1 for end-of-file).
          old_str: Required for `replace_str`; the substring to replace. Every
            occurrence is replaced and the return message reports the count.
          new_str: Replacement for `replace_str`/`replace_lines`, or the lines
            to insert for `insert`.
          insert_line: Required for `insert`; the new content is inserted AFTER
            this 1-indexed line (use 0 to insert at the top).
          start_line: Required for `replace_lines`; 1-indexed line where the
            replacement range begins (inclusive).
          end_line: Required for `replace_lines`; 1-indexed line where the
            replacement range ends (inclusive).

        Returns:
          Human-readable output describing the action taken.
        """
        _validate_abs(path)

        if command == "view":
            return await _view(path, view_range)
        if command == "create":
            if file_text is None:
                raise ToolError("`file_text` is required for `create`.")
            return await _create(path, file_text)
        if command == "delete":
            return await _delete(path)
        if command == "replace_str":
            return await _replace_str(path, old_str, new_str)
        if command == "replace_lines":
            if start_line is None or end_line is None:
                raise ToolError(
                    "`start_line` and `end_line` are required for `replace_lines`."
                )
            if new_str is None:
                raise ToolError("`new_str` is required for `replace_lines`.")
            return await _replace_lines(path, start_line, end_line, new_str)
        if command == "insert":
            if insert_line is None:
                raise ToolError("`insert_line` is required for `insert`.")
            if new_str is None:
                raise ToolError("`new_str` is required for `insert`.")
            return await _insert(path, insert_line, new_str)
        if command == "undo_edit":
            return await _undo_edit(path)

        raise ToolError(
            f"Unknown command `{command}`. Valid commands: view, create, "
            "delete, insert, replace_str, replace_lines, undo_edit."
        )

    return execute
