#!/usr/bin/env bash
# Deploy the Albion Bank Temporal.io FastAPI OpenAPI Adapter + Embedded Worker
# to Google Cloud Run.
#
# Usage:
#   GOOGLE_CLOUD_PROJECT=your-project-id ./scripts/deploy_adapter.sh

set -euo pipefail

export CLOUDSDK_CORE_DISABLE_PROMPTS=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT to your GCP project ID}"
REGION="${CLOUD_RUN_REGION:-europe-west1}"
AR_REPO="${AR_REPO:-gecx-temporal-workflow-demo}"
SERVICE_NAME="${SERVICE_NAME:-gecx-temporal-adapter}"
SA_EMAIL="${SA_EMAIL:-gecx-temporal-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
UPDATE_ENV_JSON="${UPDATE_ENV_JSON:-false}"

ACCOUNT_FLAG=()
if [[ -n "${GCLOUD_ACCOUNT:-}" ]]; then
  ACCOUNT_FLAG=("--account=${GCLOUD_ACCOUNT}")
fi

echo "==> Resolving project number for ${PROJECT_ID}..."
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} --format="value(projectNumber)")"
CES_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-ces.iam.gserviceaccount.com"
DF_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-dialogflow.iam.gserviceaccount.com"
DETERMINISTIC_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
echo "    Project Number:     ${PROJECT_NUMBER}"
echo "    CXAS Service Agent: ${CES_SERVICE_AGENT}"
echo "    Deterministic URL:  ${DETERMINISTIC_URL}"

echo "==> Ensuring Artifact Registry repository '${AR_REPO}' exists in ${REGION}..."
if ! gcloud artifacts repositories describe "$AR_REPO" ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" \
    ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --repository-format=docker \
    --description="GECX Temporal Workflow Demo container images"
fi

STAGE_DIR="$(mktemp -d "/tmp/gecx-temporal-adapter.XXXXXX")"
trap 'rm -rf "${STAGE_DIR}"' EXIT

cp "${REPO_ROOT}/pyproject.toml" "${REPO_ROOT}/uv.lock" "${STAGE_DIR}/"
cp -R "${REPO_ROOT}/temporal_service" "${STAGE_DIR}/temporal_service"
cp "${REPO_ROOT}/temporal_service/Dockerfile" "${STAGE_DIR}/Dockerfile"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
IMAGE_TAG="${IMAGE_TAG:-${GIT_SHA}-$(date -u +%Y%m%d-%H%M%S)}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:${IMAGE_TAG}"

echo "==> Building and pushing container image: ${IMAGE_URI}..."
gcloud builds submit "${STAGE_DIR}" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE_URI}"

DET_AUDIENCES="${DETERMINISTIC_URL},${DETERMINISTIC_URL}/,${DETERMINISTIC_URL}/v1/,${DETERMINISTIC_URL}/v1/dispute-workflow/verify,${DETERMINISTIC_URL}/v1/dispute-workflow/step,${DETERMINISTIC_URL}/v1/dispute-workflow/signal"

echo "==> Deploying ${SERVICE_NAME} to Cloud Run (${PROJECT_ID} / ${REGION})..."
gcloud run deploy "${SERVICE_NAME}" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE_URI}" \
  --service-account="${SA_EMAIL}" \
  --no-allow-unauthenticated \
  --min-instances=1 \
  --max-instances=1 \
  --no-cpu-throttling \
  --cpu=2 \
  --memory=2Gi \
  --timeout=3600 \
  --add-custom-audiences="${DET_AUDIENCES}" \
  --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")"

if [[ "${SERVICE_URL}" != "${DETERMINISTIC_URL}" ]]; then
  echo "==> Adding regional URL custom audiences for ${SERVICE_URL}..."
  SVC_AUDIENCES="${SERVICE_URL},${SERVICE_URL}/,${SERVICE_URL}/v1/,${SERVICE_URL}/v1/dispute-workflow/verify,${SERVICE_URL}/v1/dispute-workflow/step,${SERVICE_URL}/v1/dispute-workflow/signal"
  gcloud run services update "${SERVICE_NAME}" \
    ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --add-custom-audiences="${SVC_AUDIENCES}" \
    --quiet
fi

for SA in "${CES_SERVICE_AGENT}" "${DF_SERVICE_AGENT}" "${SA_EMAIL}"; do
  echo "==> Granting roles/run.invoker on ${SERVICE_NAME} to ${SA}..."
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"} \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null
done

if [[ "${UPDATE_ENV_JSON}" == "true" ]]; then
  ENV_FILE="${REPO_ROOT}/cxas_app/gecx-temporal-workflow-demo/environment.json"
  echo "==> Updating ${ENV_FILE} with ${DETERMINISTIC_URL}..."
  cat > "${ENV_FILE}" <<EOF
{
  "toolsets": {
    "temporal_workflow_api": {
      "openApiToolset": {
        "url": "${DETERMINISTIC_URL}"
      }
    }
  }
}
EOF
fi

echo "==> Verifying authenticated /health endpoint on ${DETERMINISTIC_URL}..."
ID_TOKEN="$(gcloud auth print-identity-token ${ACCOUNT_FLAG[@]+"${ACCOUNT_FLAG[@]}"})"
curl -fsS -H "Authorization: Bearer ${ID_TOKEN}" "${DETERMINISTIC_URL}/health"
echo ""
echo "=========================================================================="
echo "Temporal OpenAPI Adapter deployed!"
echo "Deterministic URL : ${DETERMINISTIC_URL}"
echo "Regional URL      : ${SERVICE_URL}"
echo "=========================================================================="
