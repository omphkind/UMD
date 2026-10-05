"""GUI application entry point, with explicit scriptable diagnostic modes."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys


CLI_COMMANDS = {"process", "resume", "update", "retry", "history", "export", "settings"}
CLI_FLAGS = {"--version", "--self-test", "--check-environment", "--cli"}


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    is_cli = (any(argument in CLI_FLAGS | CLI_COMMANDS for argument in arguments)
              or Path(sys.executable).stem.casefold() == "umd-console")
    if "--gui-smoke" in arguments:
        is_cli = False
    if not is_cli:
        from app.ui.gui import main as gui_main
        return gui_main(arguments)
    diagnostics_path = None
    if "--diagnostics-output" in arguments:
        index = arguments.index("--diagnostics-output")
        if index + 1 >= len(arguments):
            raise ValueError("--diagnostics-output requires a file path")
        diagnostics_path = Path(arguments[index + 1])
        del arguments[index:index + 2]
    arguments = [argument for argument in arguments if argument != "--cli"]
    from app.ui.cli import main as cli_main
    if diagnostics_path is None:
        return cli_main(arguments)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = 0
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            result = cli_main(arguments) or 0
        except SystemExit as error:
            result = error.code if isinstance(error.code, int) else 1
    report = {"ok": result == 0, "exit_code": result,
              "stdout": stdout.getvalue().strip(), "stderr": stderr.getvalue().strip()}
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
