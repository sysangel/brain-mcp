#!/usr/bin/env bash
# Build one image from origin/main and point the `brain` service and the
# `brain-nightly` job at it by digest. Env and secrets are left alone.
#
#   ops/deploy-main.sh          # plan only: what would change, and rollback targets
#   ops/deploy-main.sh --yes    # build (if the tag is new), deploy, verify
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT to your GCP project id}
REGION=${REGION:-us-central1}
REPO="$REGION-docker.pkg.dev/$PROJECT/brain/brain-mcp"
URL=${BRAIN_PUBLIC_URL:?set BRAIN_PUBLIC_URL to the service URL}
# Drop any access-token override so gcloud uses the active account.
g() { env -u CLOUDSDK_AUTH_ACCESS_TOKEN gcloud --project "$PROJECT" --quiet "$@"; }

cd "$(git rev-parse --show-toplevel)"
git fetch -q origin main
sha=$(git rev-parse --short=7 origin/main)
image="$REPO:$sha"

names() {  # env and secret names only, never values
  python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]["template"]["spec"]
spec = spec.get("template", {}).get("spec", spec)  # jobs nest one level deeper
for c in spec["containers"]:
    for e in c.get("env", []):
        kind = "secret" if "valueFrom" in e else "env"
        print(kind, e["name"])
' | sort
}

prev_rev=$(g run services describe brain --region "$REGION" --format 'value(status.latestReadyRevisionName)')
prev_svc_img=$(g run services describe brain --region "$REGION" --format 'value(spec.template.spec.containers[0].image)')
prev_job_img=$(g run jobs describe brain-nightly --region "$REGION" --format 'value(spec.template.spec.template.spec.containers[0].image)')
svc_before=$(g run services describe brain --region "$REGION" --format json | names)
job_before=$(g run jobs describe brain-nightly --region "$REGION" --format json | names)

echo "target:    origin/main $sha -> $image"
echo "service:   brain, revision $prev_rev, image $prev_svc_img"
echo "job:       brain-nightly, image $prev_job_img"
echo "rollback:  gcloud run services update-traffic brain --region=$REGION --to-revisions=$prev_rev=100"
echo "           gcloud run jobs update brain-nightly --region=$REGION --image=$prev_job_img"
[ "${1:-}" = "--yes" ] || { echo "plan only; re-run with --yes to deploy"; exit 0; }

if ! g artifacts docker images describe "$image" >/dev/null 2>&1; then
  worktree=$(mktemp -d)
  git worktree add -q --detach "$worktree" origin/main
  trap 'git worktree remove --force "$worktree"' EXIT
  # An explicit staging dir skips gcloud's default-bucket ownership check,
  # which needs storage.buckets.list on the whole project. Logs are not
  # streamed (a least-privilege deployer may not read the logs bucket); read them with
  # `gcloud logging read` if the build fails.
  (cd "$worktree" && g builds submit --tag "$image" --ignore-file=.dockerignore \
    --gcs-source-staging-dir="gs://${PROJECT}_cloudbuild/source" --suppress-logs . >/dev/null)
fi
digest=$(g artifacts docker images describe "$image" --format 'value(image_summary.digest)')
pinned="$REPO@$digest"

g run services update brain --region "$REGION" --image "$pinned" >/dev/null
g run jobs update brain-nightly --region "$REGION" --image "$pinned" >/dev/null

svc_after=$(g run services describe brain --region "$REGION" --format json | names)
job_after=$(g run jobs describe brain-nightly --region "$REGION" --format json | names)
[ "$svc_before" = "$svc_after" ] || { echo "service env/secret names changed; roll back" >&2; exit 1; }
[ "$job_before" = "$job_after" ] || { echo "job env/secret names changed; roll back" >&2; exit 1; }

echo "deployed:  $pinned (tag $sha)"
echo "revision:  $(g run services describe brain --region "$REGION" --format 'value(status.latestReadyRevisionName)')"
echo "env/secret names unchanged for service and job"
echo "health:    $(curl -fsS "$URL/health")"
