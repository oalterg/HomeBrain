#!/usr/bin/env python3
"""CI gate: exactly one shell=True call site is allowed in the codebase —
run_background_task in src/app.py, which needs the shell for log redirection
and documents its contract. Comments don't count. Anything else fails."""
import pathlib
import sys

ALLOWED = ("src/app.py", "run_background_task")

violations = []
allowed_seen = 0
for path in pathlib.Path(".").glob("**/*.py"):
    parts = path.parts
    if parts[0] not in ("src", "scripts") or "tests" in parts:
        continue
    lines = path.read_text(errors="replace").splitlines()
    for i, line in enumerate(lines, 1):
        if "shell=True" not in line or line.strip().startswith("#"):
            continue
        # The allowed site: inside run_background_task in src/app.py. Anchor on
        # the enclosing def by scanning backwards for the nearest function.
        context = next((l for l in reversed(lines[:i]) if l.startswith("def ")), "")
        if str(path) == ALLOWED[0] and ALLOWED[1] in context:
            allowed_seen += 1
            continue
        violations.append(f"{path}:{i}: {line.strip()}")

if violations:
    print("shell=True is forbidden (list-args subprocess only). Violations:")
    print("\n".join(violations))
    sys.exit(1)
if allowed_seen != 1:
    print(f"Expected exactly 1 allowed shell=True site, found {allowed_seen} — "
          "update .github/check_shell_true.py if this moved deliberately.")
    sys.exit(1)
print("shell=True gate: OK (1 documented site)")
