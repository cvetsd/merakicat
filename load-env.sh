#!/bin/bash

# Enable automatic export of all variables defined or modified in this script,
# so they are available as environment variables to the current shell and any
# child processes (equivalent to 'export VAR=value' for every assignment).
set -a

# Source the .env file located in the same directory as this script.
# $(dirname "${BASH_SOURCE[0]}") resolves to the directory containing this
# script, making the path work correctly regardless of where the script is
# called from. The .env file is expected to contain KEY=VALUE pairs.
source "$(dirname "${BASH_SOURCE[0]}")/.env"

# Disable automatic export — variables defined after this point will not be
# automatically exported to the environment.
set +a

echo "Environment variables loaded from .env"
