#!/usr/bin/env bash
# Build and deploy the Albion Bank CXAS + Temporal.io Split-Screen Demo UI to
# Google Cloud Run with Identity-Aware Proxy (IAP) enabled.
#
# Usage:
#   GOOGLE_CLOUD_PROJECT=your-project-id \
#   CXAS_APP_NAME=projects/your-project-id/locations/eu/apps/your-app-id \
#   ./scripts/deploy_ui.sh

set -euo pipefail

export CLOUDSDK_CORE_DISABLE_PROMPTS=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TMP_IAP_SETTINGS=""
STAGE_DIR=""
trap 'rm -f "$TMP_IAP_SETTINGS"; [[ -n "$STAGE_DIR" ]] && rm -rf "$STAGE_DIR"' EXIT

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT to your GCP project ID}"
REGION="${CLOUD_RUN_REGION:-europe-west1}"
AR_REPO="${AR_REPO:-gecx-temporal-workflow-demo}"
SERVICE_NAME="${SERVICE_NAME:-gecx-temporal-workflow-demo-ui}"
ADAPTER_SERVICE_NAME="${ADAPTER_SERVICE_NAME:-gecx-temporal-adapter}"
SA_EMAIL="${SA_EMAIL:-gecx-temporal-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
CXAS_LOCATION="${CXAS_LOCATION:-eu}"
CXAS_APP_RESOURCE="${CXAS_APP_NAME:?Set CXAS_APP_NAME to projects/<PROJECT_ID>/locations/<LOCATION>/apps/<APP_ID>}"
IAP_DOMAINS_CSV="${IAP_DOMAINS:-}"

ACCOUNT_FLAG=()
if [[ -n "${GCLOUD_ACCOUNT:-}" ]]; then
  ACCOUNT_FLAG=("--account=${GCLOUD_ACCOUNT}")
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} --format='value(projectNumber)')"
TEMPORAL_ADAPTER_URL="${TEMPORAL_ADAPTER_URL:-https://${ADAPTER_SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app}"

echo "==> Ensuring service account '${SA_EMAIL}' has roles/ces.client on '${PROJECT_ID}'..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/ces.client" \
  --condition=None >/dev/null

echo "==> Ensuring Artifact Registry repository '${AR_REPO}' exists in ${REGION}..."
if ! gcloud artifacts repositories describe "$AR_REPO" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" \
    ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --repository-format=docker \
    --description="GECX Temporal Workflow Demo container images"
fi

STAGE_DIR="$(mktemp -d "/tmp/gecx-temporal-ui.XXXXXX")"
cp "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/uv.lock" "${STAGE_DIR}/"
cp -R "${REPO_ROOT}/cxas_app" "${STAGE_DIR}/cxas_app"
cp -R "${REPO_ROOT}/temporal_service" "${STAGE_DIR}/temporal_service"
cp -R "${REPO_ROOT}/demo_ui" "${STAGE_DIR}/demo_ui"
cp "${REPO_ROOT}/demo_ui/Dockerfile" "${STAGE_DIR}/Dockerfile"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
IMAGE_TAG="${IMAGE_TAG:-${GIT_SHA}-$(date -u +%Y%m%d-%H%M%S)}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:${IMAGE_TAG}"

echo "==> Building and pushing container image: ${IMAGE_URI}..."
gcloud builds submit "${STAGE_DIR}" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --project="$PROJECT_ID" \
  --tag="$IMAGE_URI"

echo "==> Deploying Cloud Run service '${SERVICE_NAME}' in ${REGION} with IAP enabled..."
gcloud beta run deploy "$SERVICE_NAME" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --image="$IMAGE_URI" \
  --service-account="$SA_EMAIL" \
  --no-allow-unauthenticated \
  --iap \
  --session-affinity \
  --timeout=3600 \
  --min-instances=0 \
  --max-instances=2 \
  --memory=1Gi \
  --cpu=1 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},CXAS_LOCATION=${CXAS_LOCATION},CXAS_APP_NAME=${CXAS_APP_RESOURCE},TEMPORAL_ADAPTER_URL=${TEMPORAL_ADAPTER_URL}" \
  --quiet

echo "==> Ensuring IAP Service Agent has roles/run.invoker on '${SERVICE_NAME}'..."
gcloud beta services identity create --service=iap.googleapis.com --project="$PROJECT_ID" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} >/dev/null 2>&1 || true
IAP_SA="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="$IAP_SA" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

if [[ -n "${IAP_OAUTH_CLIENT_ID:-}" && -n "${IAP_OAUTH_SECRET_NAME:-}" ]]; then
  echo "==> Configuring IAP custom OAuth client from Secret Manager ('${IAP_OAUTH_SECRET_NAME}')..."
  if IAP_OAUTH_CLIENT_SECRET="$(gcloud secrets versions access latest --secret="$IAP_OAUTH_SECRET_NAME" --project="$PROJECT_ID" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} 2>/dev/null)"; then
    TMP_IAP_SETTINGS="$(mktemp)"
    cat > "$TMP_IAP_SETTINGS" <<EOF
accessSettings:
  oauthSettings:
    clientId: "${IAP_OAUTH_CLIENT_ID}"
    clientSecret: "${IAP_OAUTH_CLIENT_SECRET}"
EOF
    gcloud iap settings set "$TMP_IAP_SETTINGS" --project="$PROJECT_ID" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} >/dev/null
    gcloud beta iap settings set "$TMP_IAP_SETTINGS" \
      ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
      --project="$PROJECT_ID" \
      --resource-type=cloud-run \
      --region="$REGION" \
      --service="$SERVICE_NAME" >/dev/null
    rm -f "$TMP_IAP_SETTINGS"
  else
    echo "WARNING: Could not read Secret Manager secret '${IAP_OAUTH_SECRET_NAME}' in '${PROJECT_ID}'." >&2
  fi
fi

if [[ -n "${IAP_DOMAINS_CSV}" ]]; then
  echo "==> Binding IAP access (roles/iap.httpsResourceAccessor) to allowed domains..."
  IFS=',' read -ra IAP_DOMAINS <<< "${IAP_DOMAINS_CSV}"
  for DOMAIN in "${IAP_DOMAINS[@]}"; do
    DOMAIN="$(echo "${DOMAIN}" | xargs)"
    [[ -z "${DOMAIN}" ]] && continue
    echo "    - domain:${DOMAIN}"
    gcloud beta iap web add-iam-policy-binding \
      ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --resource-type=cloud-run \
      --service="$SERVICE_NAME" \
      --member="domain:${DOMAIN}" \
      --role="roles/iap.httpsResourceAccessor" \
      --quiet >/dev/null
  done
fi

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
DETERMINISTIC_UI_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
echo "=========================================================================="
echo "Albion Bank CXAS + Temporal.io Demo UI deployed with IAP protection!"
echo "Deterministic URL    : ${DETERMINISTIC_UI_URL}"
echo "Regional URL         : ${SERVICE_URL}"
echo "CXAS App Target      : ${CXAS_APP_RESOURCE}"
echo "Temporal Adapter URL : ${TEMPORAL_ADAPTER_URL}"
echo "Allowed Domains      : ${IAP_DOMAINS_CSV:-<project default>}"
echo "=========================================================================="
