# Launchd Templates

This folder contains launchd/plist templates or source snapshots copied from the existing operational setup.

Important:
- do not assume these paths are ready to install as-is
- review absolute paths before loading any plist
- prefer treating them as reference material until the repo cutover is complete

Recommended workflow:
1. validate the Python entry points from this repo
2. decide the final runtime path
3. update the plist paths deliberately
4. install only the curated plist set

Curated runtime-ready templates now include:
- `com.bala.mtm-guard.bala.plist`

For MTM guard:
- replace `__PYTHON_BIN__` with the repo `.venv` python
- replace `__MTM_GUARD_SCRIPT__` with `apps/risk/mtm_guard.py`
- replace `__REPO_ROOT__` with the repo root
- replace the log placeholders with deliberate file paths
- if you keep `KeepAlive` enabled, a Telegram `/stop` command will terminate the current process instance, but launchd may restart it
