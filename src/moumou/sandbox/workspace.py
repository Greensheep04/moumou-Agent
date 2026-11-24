import json
import os
import re
import subprocess
import sys
from pathlib import Path

from moumou.sandbox.local import LocalJail
from moumou.sandbox.process import SANDBOX_EXEC, run_command

_INSTALL = re.compile(
    r"\b(?:pip(?:\d+(?:\.\d+)?)?|pipx|brew|apt(?:-get)?|cargo|go)\s+install\b"
    r"|\buv\s+(?:pip\s+install|add|sync)\b"
    r"|\b(?:conda|mamba|poetry|npm|pnpm|yarn|bun)\s+(?:install|add|ci)\b"
    r"|\bnpx\b"
)
_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT"}
TOOL_TEMP_DIR = ".tool-tmp"


def requests_package_install(text: str) -> bool:
    return bool(_INSTALL.search(text))


def workspace_sandbox_available() -> bool:
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        return False
    try:
        result = subprocess.run(
            [SANDBOX_EXEC, "-p", "(version 1) (allow default)", "/usr/bin/true"],
            check=False, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


async def run_workspace_command(
    command, cwd: Path, timeout: float, private_root: Path, *, external_access: bool = False,
):
    jail = LocalJail(cwd)
    private_root = private_root.resolve(strict=True)
    if not jail.root.is_relative_to(private_root):
        raise PermissionError("execution workspace is outside the current run")
    scratch = jail.resolve_artifact(TOOL_TEMP_DIR)
    if scratch != jail.root / TOOL_TEMP_DIR:
        raise PermissionError("execution temporary directory aliases another path")
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in _ENV_KEYS}
    env.update(dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(scratch)))
    env["PIP_CACHE_DIR"] = str(scratch / "pip-cache")
    env["PYTHONNOUSERSITE"] = "1"
    base = json.dumps(str(private_root), ensure_ascii=False)
    work = json.dumps(str(jail.root), ensure_ascii=False)
    rules = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* (subpath {base}))",
        f"(allow file-read* (subpath {work}))",
        r'(deny file-read-data (regex #"/\.env($|\.)"))',
        "(deny file-write*)",
        f'(allow file-write* (subpath {work}) (literal "/dev/null"))',
        f"(deny file-write-unlink (literal {work}))",
        r'(deny file-write* (regex #"/\.(git|evaluation)(/|$)"))',
        "(deny file-link)",
        "(deny appleevent-send)",
        "(deny signal)",
        "(allow signal (target same-sandbox))",
    ]
    if not external_access:
        rules.extend((
            "(deny network*)",
            '(allow network-bind (local ip "localhost:*"))',
            '(allow network-inbound (local ip "localhost:*"))',
            '(allow network-outbound (remote ip "localhost:*"))',
        ))
    return await run_command(command, jail.root, timeout, env,
                             sandbox_profile="\n".join(rules))
