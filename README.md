# ct-common

Scripts e workflows compartilhados para a plataforma de Continuous Testing (TCC).

Clonado sob demanda pelos pipelines dos microsserviços `svc-*` durante a execução do CI/CD.

## Scripts

| Script | Função |
|---|---|
| `scripts/nfr-to-env.py` | Lê `nfr.yaml` → gera vars de ambiente `K6_*` (thresholds e cenários) para os scripts k6 |
| `scripts/nfr-to-chaos.py` | Lê `nfr.yaml` → gera manifests Chaos Mesh em runtime (sem manifests estáticos) |
| `scripts/evaluate-gates.py` | Compara resultados k6/chaos contra os thresholds do `nfr.yaml` → `gate-summary.json` |
| `scripts/k6-job-runner.sh` | Executa um script k6 como Kubernetes Job in-cluster |
| `scripts/chaos-experiment-runner.sh` | Executa um experimento Chaos Mesh com k6 + canary de recuperação |
| `scripts/compare-runs.py` | Compara métricas da run atual com a anterior (Azure Blob Storage) |
| `scripts/insights/` | Pipeline de insights por LLM: `collect.py`, `analyze.py`, `report.py`, `o11y-collect.py`, `code-analyze.py` |

## Workflows

| Workflow | Função |
|---|---|
| `.github/workflows/pipeline-reusable.yml` | Pipeline CI/CD completa (8 estágios), reutilizada pelos `svc-*` |
| `.github/workflows/perf-test-only-reusable.yml` | Teste de performance + insights standalone |
| `.github/workflows/resilience-test-only-reusable.yml` | Teste de resiliência standalone |
| `.github/workflows/insights-only-reusable.yml` | Análise de insights standalone |

## Uso

No `pipeline.yml` de cada `svc-*`:

```yaml
jobs:
  ci-cd:
    uses: Dorigao-LTDA/ct-common/.github/workflows/pipeline-reusable.yml@main
    with:
      service_name: catalogo
      run_stress_spike_tests: ${{ inputs.run_stress_spike_tests || false }}
      run_chaos_tests: ${{ inputs.run_chaos_tests || false }}
    secrets: inherit
```

## Variáveis geradas pelo `nfr-to-env.py`

O pipeline executa o script antes de cada estágio de teste e injeta o resultado no Job k6:

```bash
python3 scripts/nfr-to-env.py --nfr nfr.yaml --output nfr-env.sh --service catalogo
# local: source nfr-env.sh
# pipeline: sed 's/^export //' nfr-env.sh > /tmp/k6-env.txt
```

Convenção de nomes:

| Padrão | Origem no `nfr.yaml` |
|---|---|
| `K6_BUSINESS_ERRORS_THRESHOLD` | `performance.scenarios.baseline.thresholds.business_errors.rate` |
| `K6_<CENÁRIO>_EXECUTOR`, `_VUS`, `_DURATION`, `_STAGES` | parâmetros do executor do cenário; `_STAGES` vira JSON compacto |
| `K6_<CENÁRIO>_THRESHOLD_<MÉTRICA>_<AGREGADOR>` | thresholds do cenário, ex.: `http_req_duration.p95` → `THRESHOLD_HTTP_REQ_DURATION_P95` |
| `K6_<CENÁRIO>_GATE` | severidade do gate (`critical` ou `warning`) |
| `K6_CHAOS_<EXPERIMENTO>_RECOVERY_THRESHOLD` | `recovery_threshold` do experimento, em segundos, sem o prefixo do serviço (`catalogo-pod-kill` → `K6_CHAOS_POD_KILL_RECOVERY_THRESHOLD`) |
| `K8S_SIZING`, `K8S_REPLICAS`, `K8S_REQUESTS_*`, `K8S_LIMITS_*` | tier apontado por `resources.sizing` em `resources.sizing_guide` |

Saída real para o `nfr.yaml` do `svc-catalogo` (36 variáveis):

```bash
export K6_BUSINESS_ERRORS_THRESHOLD=0.05
export K6_SMOKE_EXECUTOR=constant-vus
export K6_SMOKE_VUS=1
export K6_SMOKE_DURATION=1m
export K6_SMOKE_THRESHOLD_HTTP_REQ_FAILED_RATE=0.05
export K6_SMOKE_THRESHOLD_HTTP_REQ_DURATION_P95=1000
export K6_SMOKE_GATE=warning
export K6_BASELINE_EXECUTOR=ramping-vus
export K6_BASELINE_DURATION=5m
export K6_BASELINE_STAGES=[{"target":25,"duration":"1m"},{"target":25,"duration":"3m"},{"target":0,"duration":"1m"}]
export K6_BASELINE_THRESHOLD_HTTP_REQ_FAILED_RATE=0.01
export K6_BASELINE_THRESHOLD_HTTP_REQ_DURATION_P95=300
export K6_BASELINE_THRESHOLD_HTTP_REQ_DURATION_P99=800
export K6_BASELINE_THRESHOLD_HTTP_REQS_RATE=10
export K6_BASELINE_THRESHOLD_BUSINESS_ERRORS_RATE=0.05
export K6_BASELINE_GATE=critical
export K6_STRESS_EXECUTOR=ramping-vus
export K6_STRESS_DURATION=10m
export K6_STRESS_STAGES=[{"target":500,"duration":"2m"},{"target":1000,"duration":"3m"},{"target":1500,"duration":"3m"},{"target":0,"duration":"2m"}]
export K6_STRESS_THRESHOLD_HTTP_REQ_FAILED_RATE=0.05
export K6_STRESS_THRESHOLD_HTTP_REQ_DURATION_P99=2000
export K6_STRESS_GATE=warning
export K6_SPIKE_EXECUTOR=ramping-vus
export K6_SPIKE_DURATION=5m
export K6_SPIKE_STAGES=[{"target":250,"duration":"1m"},{"target":2000,"duration":"30s"},{"target":2500,"duration":"2m"},{"target":0,"duration":"1m30s"}]
export K6_SPIKE_THRESHOLD_HTTP_REQ_FAILED_RATE=0.1
export K6_SPIKE_THRESHOLD_HTTP_REQ_DURATION_P99=3000
export K6_SPIKE_GATE=warning
export K6_CHAOS_POD_KILL_RECOVERY_THRESHOLD=30
export K6_CHAOS_NETWORK_DELAY_RECOVERY_THRESHOLD=10
export K8S_SIZING=medium
export K8S_REPLICAS=2
export K8S_REQUESTS_CPU=200m
export K8S_REQUESTS_MEMORY=256Mi
export K8S_LIMITS_CPU=500m
export K8S_LIMITS_MEMORY=1Gi
```

Os scripts k6 leem essas variáveis via `__ENV` (ex.: `__ENV.K6_BASELINE_THRESHOLD_HTTP_REQ_DURATION_P95` em `perf/baseline.js`). O `evaluate-gates.py` não consome estas variáveis: ele relê o `nfr.yaml` e compara com os artefatos k6.

## Manutenção

- **Repo canônico**: `Dorigao-LTDA/ct-common`
- Os scripts são baixados para `/tmp/ct-common` durante a execução; qualquer correção deve ser feita no repositório canônico e propaga automaticamente via `@main`.
