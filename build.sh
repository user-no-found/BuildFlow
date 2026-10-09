#!/bin/sh
set -eu
BUILD_FLOW_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$BUILD_FLOW_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec "${PYTHON:-python3}" -m buildflow "$@"
