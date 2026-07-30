"""PostToolUse hook: syntax-check any Python file Claude edits.

Reads the hook event JSON from stdin, runs py_compile on the edited file.
Exit 0 = pass (silent). Exit 2 = fail — stderr is fed back to Claude so it
fixes the syntax error immediately instead of committing it.
"""
import json
import py_compile
import sys


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    file_path = (event.get("tool_input") or {}).get("file_path", "")
    if not file_path.endswith(".py"):
        return 0

    try:
        py_compile.compile(file_path, doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"Python syntax error in {file_path}:\n{exc.msg}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
