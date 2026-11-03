#!/usr/bin/env bash
# brain-git-export --resync with the ops database URL, the export token and
# the service's exclusion env, all read inline and never printed.
#
#   ops/resync.sh                     # dry run
#   ops/resync.sh --apply --delete    # only after a human approves the dry run
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT to your GCP project id}
REGION=${REGION:-us-central1}
# Drop any access-token override so gcloud uses the active account.
g() { env -u CLOUDSDK_AUTH_ACCESS_TOKEN gcloud --project "$PROJECT" --quiet "$@"; }

cd "$(git rev-parse --show-toplevel)"
excl() {  # one env value from the Cloud Run service, empty when unset
  g run services describe brain --region "$REGION" --format json | python3 -c '
import json, sys
name = sys.argv[1]
for c in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"]:
    for e in c.get("env", []):
        if e["name"] == name and "value" in e:
            print(e["value"])
' "$1"
}
digest_ex=$(excl BRAIN_DIGEST_EXCLUDE)
episode_ex=$(excl BRAIN_EPISODE_EXCLUDE)

env DATABASE_URL="$(g secrets versions access latest --secret=brain-database-url-ops)" \
    GITHUB_TOKEN="$(g secrets versions access latest --secret=brain-github-export-token)" \
    BRAIN_BACKEND=postgres BRAIN_SYNC=0 \
    ${digest_ex:+BRAIN_DIGEST_EXCLUDE="$digest_ex"} \
    ${episode_ex:+BRAIN_EPISODE_EXCLUDE="$episode_ex"} \
    uv run brain-git-export --resync "$@"
