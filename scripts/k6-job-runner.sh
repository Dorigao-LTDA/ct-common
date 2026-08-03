#!/usr/bin/env bash
# k6-job-runner.sh — Run a k6 test as an in-cluster Kubernetes Job.
#
# Usage:
#   k6-job-runner.sh <k6_script> <service> <summary_file> <env_file> [timeout_sec] [tag]
#
#   k6_script:     relative path like perf/smoke.js (uses dirname for ConfigMap)
#   service:       e.g. pedido, catalogo, pagamento
#   summary_file:  output filename, e.g. smoke-results.json
#   env_file:      path to key=value env file (nfr-to-env.py output)
#   timeout_sec:   max wait for job completion (default: 600 = 10min)
#   tag:           optional --tag test_type=<tag> passed to k6
#
# Side effects:
#   - Creates+deletes ConfigMaps and a Job in namespace "app".
#   - Writes <summary_file> to CWD (best-effort on both success and failure).
#   - Streams k6 logs to stdout.
#   - Exits 0 when job.succeeded=1; exits 1 on failure or timeout.
set -euo pipefail

K6_SCRIPT="${1:?Usage: $0 <k6_script> <service> <summary> <env_file> [timeout] [tag]}"
SERVICE="${2:?service required}"
SUMMARY_FILE="${3:?summary filename required}"
ENV_FILE="${4:?env file path required}"
TIMEOUT="${5:-600}"
TAG="${6:-}"

SCRIPT_BASENAME="$(basename "$K6_SCRIPT")"
SCRIPT_DIR="$(dirname "$K6_SCRIPT")"
JOB_NAME="k6-${SUMMARY_FILE%.json}-$(date +%s)"
NAMESPACE="app"

# ------------------------------------------------------------------
# 1. Create ConfigMaps
# ------------------------------------------------------------------
echo "=== k6-job-runner: creating ConfigMaps for ${JOB_NAME} ==="
kubectl create configmap "${JOB_NAME}-scripts" \
  --from-file="${SCRIPT_DIR}/" \
  -n "$NAMESPACE" 2>&1

kubectl create configmap "${JOB_NAME}-env" \
  --from-env-file="$ENV_FILE" \
  -n "$NAMESPACE" 2>&1

# ------------------------------------------------------------------
# 2. Build k6 command and apply Job
# ------------------------------------------------------------------
k6_cmd="k6 run /scripts/${SCRIPT_BASENAME} --summary-export=/output/${SUMMARY_FILE}"
if [ -n "$TAG" ]; then
  k6_cmd="${k6_cmd} --tag test_type=${TAG}"
fi
echo "=== k6-job-runner: cmd=${k6_cmd} ==="

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: "${JOB_NAME}"
  namespace: "${NAMESPACE}"
  labels:
    app: k6-test
    service: "${SERVICE}"
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: k6
        image: grafana/k6:latest
        command: ["sh", "-c"]
        args:
        - "${k6_cmd}"
        envFrom:
        - configMapRef:
            name: "${JOB_NAME}-env"
        volumeMounts:
        - name: scripts
          mountPath: /scripts
        - name: output
          mountPath: /output
      volumes:
      - name: scripts
        configMap:
          name: "${JOB_NAME}-scripts"
      - name: output
        emptyDir: {}
EOF

# ------------------------------------------------------------------
# 3. Wait for pod to be running
# ------------------------------------------------------------------
echo "=== k6-job-runner: waiting for pod to be ready ==="
if ! kubectl wait --for=condition=ready pod \
    -l "job-name=${JOB_NAME}" \
    -n "$NAMESPACE" \
    --timeout=60s 2>&1; then
  echo "=== k6-job-runner: ERROR: pod did not become ready within 60s ==="
  kubectl describe pod -l "job-name=${JOB_NAME}" -n "$NAMESPACE" 2>&1 || true
  cleanup_and_exit 1
fi

POD_NAME="$(kubectl get pods -l "job-name=${JOB_NAME}" -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}')"
echo "=== k6-job-runner: pod ${POD_NAME} ready, streaming logs ==="

# ------------------------------------------------------------------
# 4. Stream + wait
# ------------------------------------------------------------------
set +e
timeout "$TIMEOUT" kubectl logs -f "$POD_NAME" -n "$NAMESPACE" 2>&1
LOG_RC=$?
set -e

if [ "$LOG_RC" -eq 124 ]; then
  echo "=== k6-job-runner: TIMEOUT after ${TIMEOUT}s ==="
fi

# ------------------------------------------------------------------
# 5. Extract summary (kubectl cp works on completed pods; exec does not)
# ------------------------------------------------------------------
echo "=== k6-job-runner: extracting ${SUMMARY_FILE} ==="
kubectl cp "${NAMESPACE}/${POD_NAME}:/output/${SUMMARY_FILE}" "./${SUMMARY_FILE}" 2>/dev/null || {
  echo "=== k6-job-runner: WARNING: kubectl cp failed, trying exec ==="
  kubectl exec "$POD_NAME" -n "$NAMESPACE" -- cat "/output/${SUMMARY_FILE}" > "./${SUMMARY_FILE}" 2>/dev/null || {
    echo "=== k6-job-runner: WARNING: could not extract summary — file may be empty ==="
    touch "./${SUMMARY_FILE}"
  }
}
echo "=== k6-job-runner: summary size: $(wc -c < "./${SUMMARY_FILE}" 2>/dev/null || echo 0) bytes ==="

# ------------------------------------------------------------------
# 6. Check job result
# ------------------------------------------------------------------
JOB_SUCCEEDED="$(kubectl get job "$JOB_NAME" -n "$NAMESPACE" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo "0")"
cleanup_and_exit() {
  local exit_code="${1:-1}"
  echo "=== k6-job-runner: cleaning up ==="
  kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
  kubectl delete configmap "${JOB_NAME}-scripts" "${JOB_NAME}-env" \
    -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
  exit "$exit_code"
}

if [ "$JOB_SUCCEEDED" = "1" ]; then
  echo "=== k6-job-runner: PASS (job succeeded) ==="
  cleanup_and_exit 0
else
  echo "=== k6-job-runner: FAIL (job did not succeed; check k6 output above) ==="
  cleanup_and_exit 1
fi
