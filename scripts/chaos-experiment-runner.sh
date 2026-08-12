#!/usr/bin/env bash
# chaos-experiment-runner.sh — Run one Chaos Mesh experiment with in-cluster k6 + canary.
#
# Usage:
#   chaos-experiment-runner.sh <manifest> <exp_name> <duration_sec> <service> <k6_env_file> <k6_script_dir>
#
#   1. Creates k6 Job (--out json NDJSON) for chaos-traffic.js
#   2. Applies chaos manifest
#   3. Health canary (port-forward curl /health) measures real outage
#   4. Waits for chaos duration
#   5. Extracts NDJSON + converts to summary JSON via ndjson-to-summary.py
#   6. Cleans up chaos resource (--wait=false) and k6 Job
#
# Writes: /tmp/chaos-results/<exp_name>.json (summary), recovery time to /tmp/chaos-recovery-times.txt
set -euo pipefail

MANIFEST="${1:?}"
EXP_NAME="${2:?}"
DURATION="${3:?}"
SERVICE="${4:?}"
K6_ENV_FILE="${5:?}"
K6_SCRIPT_DIR="${6:?}"
NAMESPACE="app"

CT_COMMON_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NDJSON_CONVERTER="${CT_COMMON_DIR}/scripts/ndjson-to-summary.py"

mkdir -p /tmp/chaos-results

# ------------------------------------------------------------------
# 1. Create k6 Job for chaos-traffic.js (--out json NDJSON)
# ------------------------------------------------------------------
K6_JOB="k6-chaos-${EXP_NAME}-$(date +%s)"
RAW_FILE="raw-${EXP_NAME}.ndjson"
SUMMARY_FILE="${EXP_NAME}.json"

echo "=== chaos-runner: creating k6 Job ${K6_JOB} ==="
kubectl create configmap "${K6_JOB}-scripts" \
  --from-file="${K6_SCRIPT_DIR}/" -n "$NAMESPACE" 2>&1
kubectl create configmap "${K6_JOB}-env" \
  --from-env-file="$K6_ENV_FILE" -n "$NAMESPACE" 2>&1

# ponytail: --out json (NDJSON) replaces deprecated --summary-export.
# Post-processing via ndjson-to-summary.py converts to summary JSON.
cat <<JOBEOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: "${K6_JOB}"
  namespace: "${NAMESPACE}"
  labels:
    app: k6-test
    chaos-experiment: "${EXP_NAME}"
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: k6
        image: grafana/k6:latest
        command: ["k6", "run", "/scripts/chaos-traffic.js",
          "--out", "json=/output/${RAW_FILE}",
          "--tag", "test_type=chaos",
          "--tag", "chaos_experiment=${EXP_NAME}"]
        envFrom:
        - configMapRef:
            name: "${K6_JOB}-env"
        volumeMounts:
        - name: scripts
          mountPath: /scripts
        - name: output
          mountPath: /output
      volumes:
      - name: scripts
        configMap:
          name: "${K6_JOB}-scripts"
      - name: output
        emptyDir: {}
JOBEOF

# Wait for pod
kubectl wait --for=condition=ready pod \
  -l "job-name=${K6_JOB}" -n "$NAMESPACE" --timeout=60s 2>&1 || true
K6_POD="$(kubectl get pods -l "job-name=${K6_JOB}" -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}')"
echo "=== chaos-runner: k6 pod ${K6_POD} ==="

sleep 3  # let k6 reach service before chaos

# ------------------------------------------------------------------
# 2. Apply chaos manifest
# ------------------------------------------------------------------
echo "=== chaos-runner: applying ${EXP_NAME} ==="
kubectl apply -f "${MANIFEST}"

# ------------------------------------------------------------------
# 3. Health canary: poll /health, detect outage window
# ------------------------------------------------------------------
echo "=== chaos-runner: monitoring recovery ==="
(
  OUTAGE_START=0
  OUTAGE_END=0
  CANARY_MAX=$(( DURATION + 300 ))
  for i in $(seq 1 ${CANARY_MAX}); do
    if curl -sf --max-time 5 http://localhost:8080/health >/dev/null 2>&1; then
      if [ "$OUTAGE_START" -ne 0 ] && [ "$OUTAGE_END" -eq 0 ]; then
        OUTAGE_END=$(date +%s)
        echo "=== Canary: recovered at t=$((i-1))s ==="
      fi
    else
      if [ "$OUTAGE_START" -eq 0 ]; then
        OUTAGE_START=$(date +%s)
        echo "=== Canary: outage detected at t=$((i-1))s ==="
      fi
    fi
    if [ "$i" -gt "$DURATION" ]; then
      if [ "$OUTAGE_START" -eq 0 ]; then break; fi
      if [ "$OUTAGE_END" -ne 0 ]; then break; fi
    fi
    sleep 1
  done
  echo "$OUTAGE_START $OUTAGE_END" > "/tmp/canary-outage-${EXP_NAME}.txt"
) &
CANARY_PID=$!

# ------------------------------------------------------------------
# 4. Wait for chaos duration while extracting k6 NDJSON in background
# ------------------------------------------------------------------
echo "=== chaos-runner: waiting ${DURATION}s for chaos ==="

# Background extraction poll (time-based, sh -c wrapper)
(
  EXTRACTED=false
  START_TS=$(date +%s)
  while true; do
    ELAPSED=$(($(date +%s) - START_TS))
    if [ "$ELAPSED" -ge $(( DURATION + 120 )) ]; then break; fi
    if timeout 5 kubectl exec "$K6_POD" -n "$NAMESPACE" -- sh -c "test -f /output/${RAW_FILE}" 2>/dev/null; then
      timeout 5 kubectl exec "$K6_POD" -n "$NAMESPACE" -- sh -c "cat /output/${RAW_FILE}" > "/tmp/chaos-results/${RAW_FILE}" 2>/dev/null && {
        python3 "$NDJSON_CONVERTER" --input "/tmp/chaos-results/${RAW_FILE}" --output "/tmp/chaos-results/${SUMMARY_FILE}" 2>/dev/null && {
          echo "=== chaos-runner: extracted+converted ${SUMMARY_FILE} at t+${ELAPSED}s ==="
          EXTRACTED=true
        }
      }
      break
    fi
    sleep 1
  done
) &
EXTRACT_PID=$!

sleep "${DURATION}"

# ------------------------------------------------------------------
# 5. Wait for k6 Job, stream logs, late extraction
# ------------------------------------------------------------------
echo "=== chaos-runner: waiting for k6 Job ==="
kubectl wait --for=condition=complete job/"${K6_JOB}" -n "$NAMESPACE" --timeout=120s 2>&1 || true
kubectl logs "$K6_POD" -n "$NAMESPACE" 2>&1 || true

wait "${EXTRACT_PID}" 2>/dev/null || true

# Late extraction if still missing
if [ ! -s "/tmp/chaos-results/${SUMMARY_FILE}" ]; then
  echo "=== chaos-runner: late extraction attempt ==="
  timeout 5 kubectl exec "$K6_POD" -n "$NAMESPACE" -- sh -c "cat /output/${RAW_FILE}" > "/tmp/chaos-results/${RAW_FILE}" 2>/dev/null || \
    timeout 5 kubectl cp "${NAMESPACE}/${K6_POD}:/output/${RAW_FILE}" "/tmp/chaos-results/${RAW_FILE}" 2>/dev/null || true
  python3 "$NDJSON_CONVERTER" --input "/tmp/chaos-results/${RAW_FILE}" --output "/tmp/chaos-results/${SUMMARY_FILE}" 2>/dev/null || \
    echo '{"metrics":{}}' > "/tmp/chaos-results/${SUMMARY_FILE}"
fi

# Cleanup k6 Job
kubectl delete job "${K6_JOB}" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
kubectl delete configmap "${K6_JOB}-scripts" "${K6_JOB}-env" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true

# ------------------------------------------------------------------
# 6. Compute recovery time from canary
# ------------------------------------------------------------------
wait "${CANARY_PID}" 2>/dev/null || true
read -r OUTAGE_START OUTAGE_END < "/tmp/canary-outage-${EXP_NAME}.txt" 2>/dev/null || true
OUTAGE_START=${OUTAGE_START:-0}
OUTAGE_END=${OUTAGE_END:-0}
if [ "$OUTAGE_START" = "0" ]; then RECOVERY_SEC=0; else
  if [ "$OUTAGE_END" = "0" ]; then RECOVERY_SEC=-1; else
    RECOVERY_SEC=$(( OUTAGE_END - OUTAGE_START ))
  fi
fi
echo "=== chaos-runner: recovery time ${RECOVERY_SEC}s ==="

# Cleanup chaos resource immediately
kubectl delete -f "${MANIFEST}" --ignore-not-found --wait=false 2>/dev/null || true

# Write recovery time
echo "${EXP_NAME}: ${RECOVERY_SEC}s" >> /tmp/chaos-recovery-times.txt
