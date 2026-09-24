#!/usr/bin/env bash
# Deploys overlays/dev to the current kubectl context, in dependency order:
#   config -> Postgres/Redis -> db-init -> migrations -> seeds -> services
# `kubectl apply -k` would create everything at once and the services would start against
# databases that don't exist yet. In production, express the same order with Helm hooks or
# Argo CD sync waves.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NS=ai-work-order
TIMEOUT="${TIMEOUT:-300s}"

if [ ! -f "$HERE/overlays/dev/deepseek.env" ]; then
  echo "missing overlays/dev/deepseek.env: copy deepseek.env.example and set MODEL_API_KEY" >&2
  exit 1
fi
echo "deploying to context: $(kubectl config current-context)"

render() { kubectl kustomize --load-restrictor LoadRestrictionsNone "$HERE/overlays/dev"; }
apply() { render | kubectl apply -l "app.kubernetes.io/component=$1" -f -; }
wait_jobs() { kubectl -n "$NS" wait --for=condition=complete job -l "app.kubernetes.io/component=$1" --timeout="$TIMEOUT"; }

# Jobs are immutable, so re-running them (new image, re-deploy) means recreating them.
kubectl -n "$NS" delete job -l 'app.kubernetes.io/component in (db-init,migration,seed)' --ignore-not-found

apply config
apply database
kubectl -n "$NS" rollout status statefulset/postgres statefulset/redis --timeout="$TIMEOUT"
apply db-init
wait_jobs db-init
apply migration
wait_jobs migration
apply seed
wait_jobs seed
apply app
kubectl -n "$NS" rollout status deployment --timeout="$TIMEOUT"

echo
echo "ready. To reach app-service:  kubectl -n $NS port-forward svc/app-service 8001:8001"
