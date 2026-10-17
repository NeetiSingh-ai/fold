#!/usr/bin/env bash
# One-shot deploy of Fold to Google Cloud. Run it in Google Cloud Shell:
#   bash deploy.sh YOUR_PROJECT_ID
# It enables the APIs, creates Firestore, deploys the API to Cloud Run (calling Gemini
# on Vertex AI with the service account, so there is no API key), adds Firebase, deploys
# Hosting + Firestore rules, and sets a budget alert.
set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="us-central1"
SERVICE="fold-api"
[ -z "$PROJECT_ID" ] && { echo "Usage: bash deploy.sh YOUR_PROJECT_ID"; exit 1; }
cd "$(dirname "$0")"
say() { printf "\n\033[1;33m== %s\033[0m\n" "$*"; }

say "1/8 Using project $PROJECT_ID"
gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

say "2/8 Enabling Google Cloud APIs (takes ~1 min)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com aiplatform.googleapis.com firebase.googleapis.com firebasehosting.googleapis.com \
  billingbudgets.googleapis.com cloudresourcemanager.googleapis.com

say "3/8 Creating Firestore database (Native mode)"
if ! gcloud firestore databases describe --database="(default)" >/dev/null 2>&1; then
  gcloud firestore databases create --database="(default)" --location=nam5 --type=firestore-native
else
  echo "Firestore already exists."
fi

say "4/8 Granting the Cloud Run service account access to Gemini, Firestore and builds"
for ROLE in roles/aiplatform.user roles/datastore.user roles/cloudbuild.builds.builder \
            roles/artifactregistry.writer roles/storage.objectViewer roles/logging.logWriter; do
  for TRY in 1 2 3 4 5; do   # retry: new projects often have concurrent IAM changes in flight
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$ROLE" \
      --condition=None -q >/dev/null 2>&1 && break
    echo "  retrying $ROLE ($TRY)"; sleep $((TRY * 5))
  done
done
echo "Done."

say "5/8 Finding a Gemini model available to this project"
TOKEN=$(gcloud auth print-access-token)
MODEL=""
for M in gemini-3.8-flash gemini-3.5-flash-lite gemini-2.5-flash; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    "https://aiplatform.googleapis.com/v1/projects/$PROJECT_ID/locations/global/publishers/google/models/$M:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"Reply with OK"}]}]}')
  echo "  $M -> HTTP $CODE"
  if [ "$CODE" = "200" ]; then MODEL="$M"; break; fi
done
[ -z "$MODEL" ] && { echo "No Gemini model responded. Wait 2 minutes (APIs may still be enabling) and re-run."; exit 1; }
case "$MODEL" in
  gemini-3.8-flash) PIN=0.75; POUT=3.75 ;;
  *)                PIN=0.30; POUT=2.50 ;;
esac
echo "Using $MODEL (\$$PIN / \$$POUT per 1M tokens in/out)"

say "6/8 Deploying the API to Cloud Run (build takes ~3-4 min)"
ADMIN_TOKEN=$(openssl rand -hex 12)
gcloud run deploy "$SERVICE" --source backend --region "$REGION" --allow-unauthenticated \
  --no-cpu-throttling --min-instances 0 --max-instances 2 --memory 1Gi --timeout 600 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,VERTEX_LOCATION=global,GEMINI_MODEL=$MODEL,PRICE_IN_PER_M=$PIN,PRICE_OUT_PER_M=$POUT,ADMIN_TOKEN=$ADMIN_TOKEN,DAILY_LLM_CAP=4000" -q
RUN_URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Cloud Run: $RUN_URL"
curl -s "$RUN_URL/api/health"; echo

say "7/8 Adding Firebase and deploying Hosting + Firestore rules"
command -v firebase >/dev/null || npm install -g firebase-tools
firebase login --no-localhost || true
firebase projects:addfirebase "$PROJECT_ID" 2>/dev/null || echo "(Firebase already added)"
if ! firebase apps:list WEB --project "$PROJECT_ID" 2>/dev/null | grep -q "fold-web"; then
  firebase apps:create WEB fold-web --project "$PROJECT_ID"
fi
# Prefer a friendly URL (fold-origamind.web.app); fall back to the project's default site.
SITE="${SITE:-fold-origamind}"
if firebase hosting:sites:get "$SITE" --project "$PROJECT_ID" >/dev/null 2>&1 || \
   firebase hosting:sites:create "$SITE" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Hosting site: $SITE"
else
  SITE="$PROJECT_ID"
  echo "Friendly site name unavailable; using default site $SITE"
fi
python3 - "$SITE" <<'PY'
import json, sys
c = json.load(open("firebase.json")); c["hosting"]["site"] = sys.argv[1]
json.dump(c, open("firebase.json", "w"), indent=2)
PY
firebase deploy --only hosting,firestore:rules,firestore:indexes --project "$PROJECT_ID" --non-interactive

say "8/8 Setting a budget alert (emails at 50/90/100% of the budget)"
BILLING=$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' | sed 's#billingAccounts/##')
CUR=$(gcloud billing accounts describe "$BILLING" --format='value(currencyCode)' 2>/dev/null || echo USD)
AMT=$([ "$CUR" = "INR" ] && echo "500INR" || echo "10USD")
gcloud billing budgets create --billing-account="$BILLING" --display-name="Fold hackathon budget" \
  --budget-amount="$AMT" --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 --filter-projects="projects/$PROJECT_ID" >/dev/null \
  && echo "Budget alert set at $AMT." || echo "Couldn't set the budget automatically; set one in Billing > Budgets & alerts."

say "DONE"
echo "Live app:        https://$SITE.web.app"
echo "Cloud Run API:   $RUN_URL"
echo "Admin token (keep private, used only for the reset endpoint): $ADMIN_TOKEN"
