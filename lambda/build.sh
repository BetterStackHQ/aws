#!/usr/bin/env bash
# Builds both Lambdas for the provided.al2023 runtime on arm64 and zips each binary as `bootstrap`
# into lambda/dist/<name>.zip: the E2E uploads them under the run id to a bucket of its own, the
# Release workflow attaches them to the release.
# Runs on an arm64 Linux host with Docker: the build happens inside Amazon Linux 2023, so the
# binaries link against the same glibc that the Lambda runtime has. Cargo's registry, the toolchain
# and the build output stay in lambda/.cache between runs.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .cache dist

docker run --rm --platform linux/arm64 -v "$PWD:/lambda" -w /lambda \
  -e CARGO_HOME=/lambda/.cache/cargo -e RUSTUP_HOME=/lambda/.cache/rustup -e CARGO_TARGET_DIR=/lambda/.cache/target \
  public.ecr.aws/amazonlinux/amazonlinux:2023 bash -euo pipefail -c '
    dnf install -y -q gcc gcc-c++ cmake zip >/dev/null
    [ -x .cache/cargo/bin/cargo ] || curl --proto =https --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal --no-modify-path
    .cache/cargo/bin/cargo build --release $([ -f Cargo.lock ] && echo --locked)
    for bin in firehose_logs_tag_enrichment firehose_metrics_tag_enrichment; do
      rm -rf dist/$bin.zip dist/tmp && mkdir dist/tmp
      cp .cache/target/release/$bin dist/tmp/bootstrap
      (cd dist/tmp && zip -q -X ../$bin.zip bootstrap)
      rm -rf dist/tmp
    done
    chown -R "$(stat -c %u:%g /lambda)" dist .cache
  '
ls -l dist
