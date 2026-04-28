"""Shell execution helpers."""

import subprocess
from pathlib import Path


def run_logged(
    cmd: list[str],
    log_file: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run *cmd* and append combined stdout+stderr to *log_file*.

    Returns (success, combined_output).
    Does not raise on non-zero exit (check=False).
    """
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout or ""
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {' '.join(cmd)}\n")
        fh.write(output)
        if not output.endswith("\n"):
            fh.write("\n")
    return result.returncode == 0, output
