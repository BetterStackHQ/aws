#!/usr/bin/env bash
# Deletes everything the E2E workflow creates: its sources in Better Stack, every e2e-gh* stack in the AWS account
# (runs are named e2e-gh<run id>a<attempt>; e2e-bootstrap is the standing OIDC stack and must stay), the buckets the
# stack retains by design, aws/spans and /aws/application-signals/data (X-Ray Transaction Search creates them outside
# the stack) and the /e2e/* log groups. The workflow runs this twice: before a run, so leftovers of a killed cleanup
# never block the next run, and after it. One run at a time per account (the workflow's concurrency group), so nothing
# with these names belongs to a live run.
# Writes a markdown report to out/cleanup.md and exits 1 when something could not be listed or deleted.
# Env: API, TEST_TEAM_TELEMETRY_API, SOURCE_PREFIX, AWS_REGION (+ AWS credentials for the account).
set -euo pipefail

mkdir -p out
: > out/cleanup.md
say() { echo "$1"; echo "$1" >> out/cleanup.md; }
could_not_list() { say "- ❌ could not list $1, so none were deleted"; touch cleanup.failed; }
rm -f cleanup.failed
say "### Cleanup"
say ""

# Sources first: while the integration role still exists Better Stack can unsubscribe cleanly.
SOURCES=$(curl -sS --fail-with-body "$API/api/v1/sources?name=$SOURCE_PREFIX&per_page=250" -H "Authorization: Bearer $TEST_TEAM_TELEMETRY_API" \
  | jq -r --arg prefix "$SOURCE_PREFIX " '.data[] | select(.attributes.name | startswith($prefix)) | "\(.id) \(.attributes.name)"') || { could_not_list sources; SOURCES=""; }
while read -r id name; do
  [ -n "$id" ] || continue
  CODE=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$API/api/v1/sources/$id" -H "Authorization: Bearer $TEST_TEAM_TELEMETRY_API")
  [ "$CODE" = 204 ] && say "- deleted source $id \`$name\`" || { say "- ❌ could not delete source $id \`$name\` (HTTP $CODE)"; touch cleanup.failed; }
done <<<"$SOURCES"

STACKS=$(aws cloudformation describe-stacks --query "Stacks[?starts_with(StackName, 'e2e-gh')].StackName" --output text) || { could_not_list stacks; STACKS=""; }
for stack in $STACKS; do
  aws cloudformation delete-stack --stack-name "$stack"
  if aws cloudformation wait stack-delete-complete --stack-name "$stack"; then say "- deleted stack \`$stack\`"; else
    say "- ❌ could not delete stack \`$stack\`: $(aws cloudformation describe-stack-events --stack-name "$stack" --query "StackEvents[?ends_with(ResourceStatus, 'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output text | head -5)"; touch cleanup.failed
  fi
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || { could_not_list buckets; ACCOUNT_ID=""; }
for bucket in "better-stack-firehose-$ACCOUNT_ID-$AWS_REGION" "better-stack-cloudtrail-$ACCOUNT_ID-$AWS_REGION"; do
  [ -n "$ACCOUNT_ID" ] && aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || continue
  if aws s3 rb "s3://$bucket" --force >/dev/null; then say "- deleted bucket \`$bucket\`"; else say "- ❌ could not delete bucket \`$bucket\`"; touch cleanup.failed; fi
done

LOG_GROUPS=$(aws logs describe-log-groups --query "logGroups[?logGroupName == 'aws/spans' || logGroupName == '/aws/application-signals/data' || starts_with(logGroupName, '/e2e/')].logGroupName" --output text) || { could_not_list "log groups"; LOG_GROUPS=""; }
for group in $LOG_GROUPS; do
  if aws logs delete-log-group --log-group-name "$group"; then say "- deleted log group \`$group\`"; else say "- ❌ could not delete log group \`$group\`"; touch cleanup.failed; fi
done

[ "$(wc -l < out/cleanup.md)" -gt 2 ] || say "- nothing to delete"
[ ! -f cleanup.failed ] || { say ""; say "❌ cleanup left resources behind"; exit 1; }
