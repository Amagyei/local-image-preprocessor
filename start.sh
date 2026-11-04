#!/bin/sh
# Starts the image service, setting up its virtual environment the first time.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
	"${PYTHON:-/opt/homebrew/bin/python3}" -m venv .venv
	.venv/bin/python -m pip install --quiet -r requirements.txt
fi

exec .venv/bin/python server.py
