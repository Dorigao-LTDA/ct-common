# ct-common

Scripts compartilhados para a plataforma de Continuous Testing (TCC).

Clonado sob demanda pelos pipelines dos microsserviços `svc-*` durante a execução do CI/CD. Se o clone falhar, cada pipeline usa a cópia local como fallback.

## Scripts

| Script | Função |
|---|---|
| `scripts/nfr-to-env.py` | Lê `nfr.yaml` → gera vars de ambiente `K6_*` para os scripts de teste k6 |
| `scripts/evaluate-gates.py` | Lê `nfr.yaml` + artefatos k6 → valida thresholds → `gate-summary.json` |

## Uso

```yaml
# No pipeline.yml de cada svc-*
- name: Clone shared toolkit
  run: git clone --depth 1 https://github.com/Dorigao-LTDA/ct-common.git /tmp/ct-common

- name: Generate k6 env from NFR
  run: python3 /tmp/ct-common/scripts/nfr-to-env.py --nfr nfr.yaml --output nfr-env.sh --service ${{ env.SERVICE_NAME }}

- name: Validate against NFR
  run: python3 /tmp/ct-common/scripts/evaluate-gates.py --artifacts . --nfr nfr.yaml --service ${{ env.SERVICE_NAME }}
```

## Manutenção

- **Repo canônico**: `Dorigao-LTDA/ct-common`
- Cópias locais em `svc-*/scripts/` são **fallbacks** — nunca editar diretamente sem sincronizar com ct-common.
