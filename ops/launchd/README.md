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

