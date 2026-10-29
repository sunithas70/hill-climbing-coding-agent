"""Coding agent with file discovery, search, read/write, and sandboxed execution tools."""

import os
import re
import subprocess
import sys
import warnings

from google.adk.agents import Agent
from google.genai import types

# The default model, shared with eval.py so there is a single source of truth.
DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Silencing an ADK warning to declutter terminal output
warnings.filterwarnings("ignore", message=r".*JSON_SCHEMA_FOR_FUNC_DECL.*", category=UserWarning)


def _resolve(sandbox: str, path: str) -> str:
    """Turn a sandbox-relative path into an absolute one, refusing to escape the sandbox."""
    sandbox = os.path.realpath(sandbox)
    full = os.path.realpath(os.path.join(sandbox, path))
    # Allow only the sandbox root itself or a path beneath it (blocks "../" escapes).
    if full != sandbox and not full.startswith(sandbox + os.sep):
        raise ValueError(f"path escapes sandbox: {path!r}")
    return full


# The helpers below return errors as plain strings instead of raising, so the
# model sees what went wrong and can recover on its next turn.

def _read(sandbox: str, path: str) -> str:
    try:
        with open(_resolve(sandbox, path), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return f"Error: no such file: {path}"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _write(sandbox: str, path: str, contents: str) -> str:
    try:
        full = _resolve(sandbox, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(contents)
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Wrote {len(contents)} chars to {path}"


def _list_files(sandbox: str, directory: str = ".") -> str:
    try:
        root = _resolve(sandbox, directory)
        if not os.path.exists(root):
            return f"Error: no such directory: {directory}"
        sandbox_real = os.path.realpath(sandbox)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in ("__pycache__", ".git", ".venv")
            )
            for fname in sorted(filenames):
                if fname.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fname)
                found.append(os.path.relpath(full, sandbox_real))
        return "\n".join(found) if found else "(empty directory)"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _search_files(sandbox: str, pattern: str, directory: str = ".") -> str:
    try:
        root = _resolve(sandbox, directory)
        if not os.path.exists(root):
            return f"Error: no such directory: {directory}"
        sandbox_real = os.path.realpath(sandbox)
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in ("__pycache__", ".git", ".venv")
            )
            for fname in sorted(filenames):
                if fname.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, sandbox_real)
                try:
                    with open(full, encoding="utf-8") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if regex.search(line):
                                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n".join(matches) if matches else "No matches found."
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _run_command(sandbox: str, command: str, stdin_data: str = "") -> dict:
    try:
        sandbox_real = os.path.realpath(sandbox)
        src_dir = os.path.join(sandbox_real, "src")
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": f"{src_dir}:{sandbox_real}",
            "PATH": f"{os.path.dirname(sys.executable)}:{os.environ.get('PATH', '')}",
        }
        proc = subprocess.run(
            command,
            shell=True,
            cwd=sandbox_real,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "Error: command timed out"}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": f"Error: {exc}"}


SYSTEM_INSTRUCTION = """You are an autonomous software engineering agent. Follow this strict workflow on every task:

1. EXPLORE & TRACE UPSTREAM ROOT CAUSES (READ BEFORE WRITING):
   - Always call `list_files` first (and `search_files` as needed) to discover every file in the project (`src/`, packages, `__init__.py`, and sibling helper modules).
   - Never assume the file mentioned in the prompt contains the actual bug. Read ALL files in the package (especially upstream parsing, loading, and data-ingestion modules) and trace the full data flow from raw input to final output before editing anything.
   - Fix the root cause where data is first corrupted or truncated, not the downstream symptom.
   - When fixing existing functions or libraries, preserve their existing function signatures and return types (e.g., if a function returns `float` or `int`, keep returning `float` or `int` rather than changing its return type to `Decimal`).

2. ENUMERATE BOUNDARY CONDITIONS & OUTPUT CONTRACTS:
   Before writing or finalizing code, systematically enumerate and handle all boundary conditions and formatting rules:
   - Sequence Direction & Ordering: Check ascending, single-element, equal-bound (`a == b`), and descending (`a > b`) inputs. For any range `a-b`, if `a > b` (such as `9-7`), expand in descending order (`9 8 7`, using step `-1`) rather than producing an empty sequence.
   - Numeric Precision & Half-Up Rounding: In CLI programs that compute money or decimal lengths, parse numbers with `decimal.Decimal(str_value)` (never `float`) and round to two decimal places using `Decimal("0.01")` with `rounding=decimal.ROUND_HALF_UP` (e.g., `5.005 -> 5.01`, `3.675 -> 3.68`). When summing `Decimal` objects, always initialize the sum with `Decimal("0")` (`sum(items, Decimal("0"))`) so empty inputs yield `Decimal("0")` instead of integer `0` (avoiding `AttributeError` on `.quantize()`).
   - Trailing-Zero Trimming: When a specification requires plain decimals with trailing zeros trimmed (e.g., `4.0 -> 4`, `1.50 -> 1.5`), format with fixed-point `s = f"{val:f}"` and strip trailing zeros only when a decimal point is present (`s = s.rstrip("0").rstrip(".") if "." in s else s`, normalizing `"-0"` or `""` to `"0"`), ensuring integer zeros like `10` are preserved.
   - Empty & Blank Inputs: Explicitly test empty `stdin` (`""` and `"\\n"`). When the specification states "with empty input, print nothing" or when a list/merge command has zero output lines, write zero bytes to `stdout` (do NOT call `print("")`, which emits an unwanted `"\\n"`). When an aggregate command (`total`, `length`, `average`) runs on empty input, print `0.00` without crashing (`ZeroDivisionError`). Ignore blank lines in multi-line input.
   - Exit Codes & Stderr Discipline: On malformed input lines (wrong number of fields, non-numeric values, invalid intervals where `start > end`) or usage errors (missing command, unknown command, missing required arguments), write the error message to `sys.stderr` (`print(..., file=sys.stderr)`), emit nothing on `stdout`, and exit with the exact required code (`sys.exit(2)` for usage/input errors; `sys.exit(1)` when a queried category/item is missing).

3. STRICT TOOL-USE & VERIFICATION DISCIPLINE:
   - CRITICAL: Code written in your chat response has NO effect on the filesystem. You MUST call `write_file` on the target file under `src/` to apply every fix or create every program. Never claim a file is updated unless you actually called `write_file` on that file path.
   - Do not create temporary reproduction files with `write_file`; instead, run one-liners directly via `run_command` (e.g., `python3 -c "..."`) to reproduce bugs and test fixes.
   - As soon as you locate an upstream bug, immediately call `write_file` on the buggy `src/...` file to fix it, then call `run_command` to verify end-to-end behavior.
   - Test every command and edge case via `run_command` (checking `stdout`, `stderr`, and `returncode` on normal examples, descending ranges, touching intervals, half-up rounding ties, empty `stdin`, unknown commands, and malformed inputs).
   - If any test fails, update the `src/...` file with `write_file` and re-run `run_command` until all checks pass. Do not create or modify files inside `tests/`."""


def build_agent(sandbox: str, model: str) -> Agent:
    # Each tool's docstring is the description the model sees, so it states that
    # paths are sandbox-relative.
    def list_files(directory: str = ".") -> dict:
        """List all files under a directory (relative to the project root) to discover the codebase layout."""
        return {"result": _list_files(sandbox, directory)}

    def search_files(pattern: str, directory: str = ".") -> dict:
        """Search for a regex or text pattern across files under a directory (relative to the project root)."""
        return {"result": _search_files(sandbox, pattern, directory)}

    def read_file(path: str) -> dict:
        """Read a text file (path is relative to the project root) and return its contents."""
        return {"result": _read(sandbox, path)}

    def write_file(path: str, contents: str) -> dict:
        """Create or overwrite a text file (path is relative to the project root)."""
        return {"result": _write(sandbox, path, contents)}

    def run_command(command: str, stdin_data: str = "") -> dict:
        """Execute a shell or Python command inside the sandbox with optional stdin_data, returning returncode, stdout, and stderr."""
        return _run_command(sandbox, command, stdin_data)

    return Agent(
        name="coding_agent",
        model=model,
        instruction=SYSTEM_INSTRUCTION,
        tools=[list_files, search_files, read_file, write_file, run_command],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW
            )
        )
    )

if __name__ == "__main__":
    # Test the agent on a prompt in ./out sandbox.  Usage: python agent.py "your prompt"
    import asyncio
    from google.adk.runners import InMemoryRunner

    # Default to Vertex AI (like eval.py) so `python agent.py` works without a
    # Gemini API key; an existing env value, if set, takes precedence.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp-01-a0516ea8dbfb")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    sandbox = os.path.join(os.path.dirname(os.path.realpath(__file__)), "out")
    os.makedirs(os.path.join(sandbox, "src"), exist_ok=True)
    prompt = " ".join(sys.argv[1:]) or "Write a hello world python script"

    async def _run_agent() -> None:
        runner = InMemoryRunner(agent=build_agent(sandbox, DEFAULT_MODEL))
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="me")
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        async for event in runner.run_async(user_id="me", session_id=session.id, new_message=message):
            for part in (event.content and event.content.parts) or []:
                print(part.text or "", end="", flush=True)
    asyncio.run(_run_agent())
    import glob
    for script in glob.glob(f"{sandbox}/**/*.py", recursive=True):
        print(f"\n=== output of {os.path.relpath(script, sandbox)} ===\n" + subprocess.run([sys.executable, script], cwd=sandbox, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout, end="")
