"""Shell command execution tool."""

from __future__ import annotations

import re

from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output
from openclose.util.process import find_bash, run
from openclose import flag

# Patterns for dangerous bash commands (best-effort heuristics).
#
# Goal: block what an autonomous agent could realistically use to cause real,
# hard-to-recover damage (system destruction, privilege escalation, remote
# code exec, reverse shells). Do NOT block ordinary developer commands.
#
# Patterns are matched against BOTH the raw command and a normalized form
# (see _normalize_for_check). The real security boundary is the permission
# engine — this is just a coarse net for the most obviously catastrophic
# strings.

# Start-of-statement anchor: command begins the input, follows a separator,
# or follows && / ||. Avoids false-firing on embedded literals like
# `echo 'rm -rf /etc' > note.txt` while still catching chains like
# `cd /tmp && rm -rf /etc`.
_STMT_START = r"(?:^|[;&|`(\n]|&&|\|\|)\s*"

# FHS roots that warrant blocking when targeted by `rm -rf`, `find -delete`,
# recursive `chmod`/`chown`. Excludes /tmp, /home, /mnt, /media, /srv where
# developer trees commonly live. /opt matches only when bare (lookahead).
_SENSITIVE_ROOT = (
    r"(?:/(?:\s|$|;|&|\|)"                          # bare /
    r"|~(?:/|\s|$|;|&|\|)"                          # bare ~ or ~/
    r"|\$HOME(?:/|\s|$|;|&|\|)"                     # $HOME
    r"|/(?:etc|usr|var|bin|sbin|lib|lib32|lib64"
    r"|boot|root|sys|proc|dev)(?:/|\s|$|;|&|\|)"
    r"|/opt(?:\s|$|;|&|\|))"                        # bare /opt only
)

# rm flag clusters (short -rf/-fr/-Rf/-rfv variants OR long --recursive/--force).
_RM_RECURSIVE_FORCE = (
    r"rm\s+"
    r"(?:"
    r"-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*"               # -rf, -Rf, -rfv
    r"|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*"              # -fr, -fRv
    r"|(?:--(?:recursive|force|no-preserve-root)\b\s*)+"
    r"(?:-[a-zA-Z]*[rRf][a-zA-Z]*\s+)?"
    r"(?:--(?:recursive|force|no-preserve-root)\b\s*)*"
    r")"
    r"\s+(?:-\S+\s+)*"                              # extra flags
)

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- Filesystem destruction ----------------------------------------
    (re.compile(_STMT_START + _RM_RECURSIVE_FORCE + _SENSITIVE_ROOT),
     "rm -rf on system paths is blocked"),
    (re.compile(r"\brm\b[^\n]*--no-preserve-root\b"),
     "rm --no-preserve-root is blocked"),
    (re.compile(_STMT_START + r"find\s+" + _SENSITIVE_ROOT + r"[^\n]*-delete\b"),
     "find -delete on system paths is blocked"),
    (re.compile(_STMT_START + r"find\s+" + _SENSITIVE_ROOT + r"[^\n]*-exec\s+rm\b"),
     "find -exec rm on system paths is blocked"),
    (re.compile(_STMT_START + r"chmod\s+(?:-[a-zA-Z]*R[a-zA-Z]*|--recursive)\s+"
                r"[0-7]{3,4}\s+" + _SENSITIVE_ROOT),
     "recursive chmod on system paths is blocked"),
    (re.compile(_STMT_START + r"chown\s+(?:-[a-zA-Z]*R[a-zA-Z]*|--recursive)\s+"
                r"\S+\s+" + _SENSITIVE_ROOT),
     "recursive chown on system paths is blocked"),

    # --- Privilege escalation ------------------------------------------
    (re.compile(r"\bsudo\b"), "sudo commands are blocked"),
    (re.compile(_STMT_START + r"su\s"), "su commands are blocked"),
    (re.compile(r"\bpkexec\b"), "pkexec is blocked"),
    (re.compile(r"\bdoas\b"), "doas is blocked"),

    # --- Remote code execution -----------------------------------------
    (re.compile(r"\b(?:curl|wget|fetch)\b[^\n]*\|\s*(?:ba|z|k|d|a)?sh\b"),
     "piping a downloader to a shell is blocked"),
    (re.compile(r"(?:\b(?:bash|sh|zsh|source)\b|^\s*\.\s)[^\n]*<\(\s*(?:curl|wget|fetch)\b"),
     "executing process substitution from a downloader is blocked"),
    (re.compile(r"\beval\b[^\n]*[\"`]?\$\(\s*(?:curl|wget|fetch)\b"),
     "eval of downloader output is blocked"),
    (re.compile(r"\beval\b[^\n]*`\s*(?:curl|wget|fetch)\b"),
     "eval of downloader output is blocked"),

    # --- Reverse shells ------------------------------------------------
    (re.compile(r"/dev/tcp/"), "/dev/tcp redirection (reverse shell) is blocked"),
    (re.compile(r"/dev/udp/"), "/dev/udp redirection (reverse shell) is blocked"),
    (re.compile(r"\bnc\b[^\n]*\s-[a-zA-Z]*e[a-zA-Z]*\s"),
     "nc -e (reverse shell) is blocked"),
    (re.compile(r"\bncat\b[^\n]*--exec\b"),
     "ncat --exec (reverse shell) is blocked"),

    # --- Fork bomb -----------------------------------------------------
    (re.compile(r":\(\)\s*\{[^}]*\|[^}]*\}\s*;\s*:"),
     "fork bomb is blocked"),

    # --- Mass kill -----------------------------------------------------
    (re.compile(r"\bkill\s+(?:-(?:[19]|KILL|SIGKILL)\s+)+-1\b"),
     "kill -9 -1 (mass kill) is blocked"),
    (re.compile(r"\bpkill\s+(?:-(?:[19]|KILL|SIGKILL)\s+)+-1\b"),
     "pkill -9 -1 (mass kill) is blocked"),
    (re.compile(r"\bkillall\s+(?:-(?:[19]|KILL|SIGKILL)\s+)"),
     "killall -9 is blocked"),

    # --- Disk operations -----------------------------------------------
    (re.compile(r">\s*/dev/(?:sd|hd|nvme\d+n|mmcblk\d+|vd|xvd|loop)"),
     "writing to a raw block device is blocked"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(?:sd|hd|nvme|mmcblk|vd|xvd)"),
     "dd to a block device is blocked"),
    (re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b"),
     "mkfs is blocked"),
    (re.compile(r"\bwipefs\b"),
     "wipefs is blocked"),
    (re.compile(r"\bshred\b[^\n]*\s/dev/"),
     "shred on a device is blocked"),
    (re.compile(r"\bfdisk\b[^\n]*\s/dev/"),
     "fdisk on a device is blocked"),
    (re.compile(r"\b(?:parted|sgdisk|gdisk)\b[^\n]*\s/dev/"),
     "partition tools on a device are blocked"),
]


# --- Bypass-hardening normalization -----------------------------------------

_IFS_RE = re.compile(r"\$\{IFS\}|\$IFS\b")
_BACKSLASH_LETTER_RE = re.compile(r"\\([A-Za-z])")
# Same-quote pair around a bare alphanum/`._-` token (no whitespace inside).
_QUOTED_TOKEN_RE = re.compile(r"(?<!\$)(['\"])([A-Za-z][A-Za-z0-9_.-]*)\1")


def _normalize_for_check(command: str) -> str:
    """Best-effort textual normalization to defeat trivial obfuscation.

    NOT a bash parser — only four cheap transforms an LLM might naively try
    as a bypass: ${IFS} → space, \\<letter> → <letter>, strip same-quote
    pairs around alphanum tokens. Run twice to handle `s'u'do`-style nesting.
    Pattern matching uses raw OR normalized — normalization can only ADD
    blocks, never remove them.
    """
    s = _IFS_RE.sub(" ", command)
    s = _BACKSLASH_LETTER_RE.sub(r"\1", s)
    for _ in range(2):
        s = _QUOTED_TOKEN_RE.sub(r"\2", s)
    return s


def _check_command(command: str) -> str | None:
    """Check if a command matches dangerous patterns.

    Matches against the raw command AND a normalized form (defeats trivial
    quoting / IFS / backslash-escape bypasses). Returns the block reason if
    matched, None otherwise. Best-effort — the real security boundary is the
    permission engine.
    """
    normalized = _normalize_for_check(command)
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command) or (normalized != command and pattern.search(normalized)):
            return reason
    return None


def make_bash_tool(project_dir: str = ".") -> Tool:
    """Create the bash execution tool."""

    async def execute(
        command: str = "",
        timeout: int = 0,
        **kwargs: object,
    ) -> ToolResult:
        if not command.strip():
            return ToolResult(error="Empty command")

        blocked = _check_command(command)
        if blocked:
            return ToolResult(error=f"Blocked: {blocked}")

        bash_path = find_bash()
        if bash_path is None:
            return ToolResult(error=(
                "bash not found. The bash tool requires bash; on "
                "Windows install Git Bash (https://gitforwindows.org/) or use "
                "WSL. For non-shell work, prefer read/write/edit/glob/grep tools."
            ))

        timeout_ms = timeout if timeout > 0 else flag.BASH_DEFAULT_TIMEOUT_MS
        timeout_s = timeout_ms / 1000.0

        result = await run(
            bash_path, "-c", command,
            cwd=project_dir,
            timeout=timeout_s,
        )

        output_parts: list[str] = []

        # Header: echo the command and cwd for LLM context.
        output_parts.append(f"$ {command}")
        output_parts.append(f"[cwd: {project_dir}]")

        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")

        if result.timed_out:
            output_parts.append(f"[timed out after {timeout_s}s]")
        elif not result.timed_out and result.duration >= 1.0:
            output_parts.append(f"[duration: {result.duration:.1f}s]")

        output = "\n".join(output_parts)

        # Error field — distinct from output markers, avoids duplication.
        if result.timed_out:
            error_msg = f"Command timed out after {timeout_s}s (timeout was {timeout_ms}ms)"
        elif not result.ok:
            error_msg = f"Command failed with exit code {result.returncode}"
        else:
            error_msg = ""

        return ToolResult(
            output=truncate_output(output),
            error=error_msg,
            metadata={
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "duration": result.duration,
            },
        )

    return Tool(
        name="bash",
        description=(
            "USE IT TO RUN SHELL-ONLY COMMANDS — running tests, build steps, git, "
            "package managers, process management. From the project directory"
            "Returns stdout/stderr plus exit status. "
            "Requires `bash` on PATH (Git Bash or WSL on Windows). "
            "Dangerous commands are blocked for safety. "
            "Never loop on failed on failed install commands to chase missing dependencies in "
            "sandboxed environments — report the failure and stop."
        ),
        parameters=[
            ToolParameter(
                name="command",
                description=(
                    "Bash command line to execute. Runs via `bash -c` from the "
                    "project working directory. Working directory does not "
                    "persist across calls — chain dependent steps with `&&` in a "
                    "single call."
                ),
            ),
            ToolParameter(
                name="timeout",
                type="integer",
                description=(
                    "Maximum runtime in milliseconds before the command is "
                    "killed. Set explicitly for commands (tests, builds)."
                ),
                required=False,
                default=0,
            ),
        ],
        execute_fn=execute,
    )
