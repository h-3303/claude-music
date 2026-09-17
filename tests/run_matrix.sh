#!/usr/bin/env bash
# Run the test suite against every supported Nicotine+ version (one process per version, since
# pynicotine is a set of process-wide singletons). Extra arguments are passed to pytest.
set -uo pipefail
cd "$(dirname "$0")/.."

refs=${NICOTINE_PLUS_REFS:-"3.3.10 master"}
failed=()

for ref in $refs; do
  echo "=== Nicotine+ $ref"
  NICOTINE_PLUS_REF=$ref uv run pytest "$@" || failed+=("$ref")
done

if ((${#failed[@]})); then
  echo "FAILED for: ${failed[*]}"
  exit 1
fi
echo "All Nicotine+ versions passed: $refs"
