#!/bin/sh
exec "$FAKE_PY" "$(dirname "$0")/fake_llama.py" "$@"
