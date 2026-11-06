#!/usr/bin/env bash
# Packages both Lambdas as the zips the template deploys: lambda/dist/<function>.zip, each holding the function's
# source file at its root, where the Ruby runtime loads <handler file>.rb from. The E2E workflow uploads them to a
# bucket of the run's own and the release workflow attaches them to the GitHub Release.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist && mkdir dist
for fn in firehose_logs_tag_enrichment firehose_metrics_tag_enrichment; do
  zip -q -X -j "dist/$fn.zip" "$fn.rb"
done
ls -l dist
