#!/bin/bash
# Quick MCX scan - run from anywhere
cd "$(dirname "$0")/../.." || exit 1
python3 Tools/mcx_scanner/mcx_scanner.py "$@"
