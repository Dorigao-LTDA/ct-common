#!/usr/bin/env python3
"""
Gate evaluation: read nfr.yaml thresholds + k6 test artifacts, validate gates.
Makes nfr.yaml the single source of truth for all threshold values.

Usage: python3 scripts/evaluate-gates.py --artifacts <dir> --nfr nfr.yaml --service <name>
Exit 1 if any CRITICAL gate fails. Output: <artifacts>/gate-summary.json
"""
import argparse
import json
import os
import re
import subprocess
import sys


def parse_yaml(path):
    """Parse YAML with PyYAML (preinstalled on ubuntu-24.04)."""
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def load_k6_result(path):
    """k6 outputs newline-delimited JSON; metrics are in the last line."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        lines = f.readlines()
    if not lines:
        return None
    return json.loads(lines[-1].strip())


# --- Kubernetes resource quantity parsing (stdlib only) ---
# ponytail: covers only what nfr.yaml/values emit (m, Ki/Mi/Gi, plain int).
# Not full k8s resource.Quantity — no decimal-suffix memory, no scientific notation.
_CPU_SUFFIXES = {'m': 0.001}
_MEM_BINARY = {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4}
_MEM_DECIMAL = {'k': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4}


def parse_cpu(s):
    """Return CPU in millicores (int). 500m -> 500; 1 -> 1000; 0.5 -> 500."""
    if s is None or s == '':
        return None
    s = str(s).strip()
    if s.endswith('m'):
        return int(s[:-1])
    val = float(s)
    return int(val * 1000)


def parse_mem(s):
    """Return memory in bytes (int). 1Gi -> 1073741824; 512Mi -> 536870912."""
    if s is None or s == '':
        return None
    s = str(s).strip()
    for suffix, mult in _MEM_BINARY.items():
        if s.endswith(suffix):
            return int(float(s[:-len(suffix)])) * mult
    for suffix, mult in _MEM_DECIMAL.items():
        if s.endswith(suffix):
            return int(float(s[:-len(suffix)])) * mult
    return int(float(s))


def get_cluster_state(service, namespace='app'):
    """Read live Deployment resources via kubectl. Return dict or None on failure."""
    # ponytail: jsonpath with -o json is simpler than parsing jsonpath output.
    cmd = [
        'kubectl', 'get', 'deployment', service,
        '-n', namespace, '-o', 'json', '--ignore-not-found',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        deploy = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    spec = deploy.get('spec', {})
    containers = spec.get('template', {}).get('spec', {}).get('containers', [])
    if not containers:
        return None
    c = containers[0]
    res = c.get('resources', {})
    return {
        'replicas': spec.get('replicas', 0),
        'requests': res.get('requests', {}),
        'limits': res.get('limits', {}),
    }


def check_sizing_drift(nfr, service, check_fn, namespace='app'):
    """
    Compare declared sizing tier (nfr.yaml) vs live Deployment resources.
    Adds gates: replicas, requests.{cpu,memory}, limits.{cpu,memory}.
    Returns True if all drift checks added; False if skipped (no live state).
    """
    resources_cfg = nfr.get('resources', {})
    sizing = resources_cfg.get('sizing')
    if not sizing:
        print('\n--- Sizing Drift Gate: no `resources.sizing` declared in nfr.yaml ---')
        return False

    tier = resources_cfg.get('sizing_guide', {}).get(sizing)
    if not tier:
        print(f'\n--- Sizing Drift Gate: tier "{sizing}" not found in sizing_guide ---')
        return False

    print(f'\n--- Sizing Drift Gate (tier={sizing}) ---')

    actual = get_cluster_state(service, namespace)
    if actual is None:
        print('  WARNING: could not read live deployment (kubectl unavailable or not deployed)')
        return False

    # Replicas
    expected_replicas = tier.get('replicas')
    actual_replicas = actual.get('replicas')
    check_fn('replicas', actual_replicas, '==', expected_replicas,
             'critical', 'sizing_replicas')

    # requests + limits (CPU in millicores, memory in bytes)
    for kind in ('requests', 'limits'):
        expected = tier.get(kind, {})
        actual_kind = actual.get(kind, {})
        for resource in ('cpu', 'memory'):
            exp_val = expected.get(resource)
            act_val = actual_kind.get(resource)
            if exp_val is None or act_val is None:
                continue
            if resource == 'cpu':
                exp_norm = parse_cpu(exp_val)
                act_norm = parse_cpu(act_val)
            else:
                exp_norm = parse_mem(exp_val)
                act_norm = parse_mem(act_val)
            metric_name = f'{kind}.{resource}'
            # Display raw values (more readable), compare normalized
            check_fn(metric_name, act_norm, '==', exp_norm,
                     'critical', f'sizing_{kind}_{resource}')
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Evaluate NFR gates against test results'
    )
    parser.add_argument('--artifacts', required=True,
                        help='Directory with test artifacts')
    parser.add_argument('--nfr', required=True, help='Path to nfr.yaml')
    parser.add_argument('--service', required=True,
                        help='Service name (catalogo, pagamento, pedido)')
    parser.add_argument('--namespace', default='app',
                        help='Namespace where the service is deployed')
    parser.add_argument('--skip-sizing-drift', action='store_true',
                        help='Skip sizing drift check (kubectl vs nfr.yaml)')
    args = parser.parse_args()

    nfr = parse_yaml(args.nfr)
    service = args.service
    summary = {'service': service, 'status': 'PASSED', 'gates': []}

    def check(metric_name, actual, operator_str, threshold, severity, gate_name):
        passed = False
        try:
            if operator_str == '<':
                passed = float(actual) < float(threshold)
            elif operator_str == '<=':
                passed = float(actual) <= float(threshold)
            elif operator_str == '>=':
                passed = float(actual) >= float(threshold)
            elif operator_str == '>':
                passed = float(actual) > float(threshold)
            elif operator_str == '==':
                passed = float(actual) == float(threshold)
        except (ValueError, TypeError):
            passed = False

        result = {
            'gate': gate_name,
            'metric': metric_name,
            'actual': actual,
            'threshold': f'{operator_str} {threshold}',
            'status': 'PASS' if passed else 'FAIL',
            'severity': severity,
        }
        summary['gates'].append(result)
        icon = '\u2705' if passed else '\u274C'
        print(f'  {icon} {metric_name}: {actual} {operator_str} {threshold}  '
              f'\u2192 {result["status"]} ({severity})')
        if not passed and severity == 'critical':
            summary['status'] = 'FAILED'

    # ============================================================
    # Smoke Gate
    # ============================================================
    smoke_path = os.path.join(args.artifacts, 'smoke-results',
                              'smoke-results.json')
    smoke = load_k6_result(smoke_path)
    if smoke:
        print('\n--- Smoke Gate ---')
        metrics = smoke.get('metrics', {})
        sm_cfg = (nfr.get('performance', {}).get('scenarios', {})
                  .get('smoke', {}).get('thresholds', {}))
        sm_gate = sm_cfg.pop('gate', 'warning') if isinstance(
            sm_cfg, dict) else 'warning'

        failed = metrics.get('http_req_failed', {}).get('values', {}).get('rate', 0)
        if isinstance(sm_cfg, dict) and 'http_req_failed' in sm_cfg:
            check('http_req_failed', failed, '<',
                  sm_cfg['http_req_failed'].get('rate', 1),
                  sm_gate, 'smoke_http_req_failed')

        p95 = metrics.get('http_req_duration', {}).get('values', {}).get('p(95)', 0)
        if (isinstance(sm_cfg, dict)
                and 'http_req_duration' in sm_cfg
                and 'p95' in sm_cfg['http_req_duration']):
            check('http_req_duration.p95', p95, '<',
                  sm_cfg['http_req_duration']['p95'],
                  sm_gate, 'smoke_p95')
    else:
        print('\n--- Smoke Gate: results not available ---')

    # ============================================================
    # Baseline Gate (CRITICAL)
    # ============================================================
    baseline_path = os.path.join(args.artifacts, 'perf-results',
                                 'baseline-results.json')
    baseline = load_k6_result(baseline_path)
    if baseline:
        print('\n--- Baseline Gate (CRITICAL) ---')
        metrics = baseline.get('metrics', {})
        bl = (nfr.get('performance', {}).get('scenarios', {})
              .get('baseline', {}).get('thresholds', {}))
        bl_gate = bl.get('gate', 'critical') if isinstance(bl, dict) else 'critical'

        # http_req_failed
        failed_rate = metrics.get('http_req_failed', {}).get('values', {}).get('rate', 0)
        th = bl.get('http_req_failed', {})
        check('http_req_failed', failed_rate, '<',
              th.get('rate', 0.01), bl_gate,
              'baseline_http_req_failed')

        # http_req_duration p95
        p95 = metrics.get('http_req_duration', {}).get('values', {}).get('p(95)', 0)
        th = bl.get('http_req_duration', {})
        check('http_req_duration.p95', p95, '<',
              th.get('p95', 300), bl_gate,
              'baseline_p95')

        # http_req_duration p99
        p99 = metrics.get('http_req_duration', {}).get('values', {}).get('p(99)', 0)
        th = bl.get('http_req_duration', {})
        check('http_req_duration.p99', p99, '<',
              th.get('p99', 800), bl_gate,
              'baseline_p99')

        # throughput (per-scenario: http_reqs.rate, not throughput.min)
        throughput = metrics.get('http_reqs', {}).get('values', {}).get('rate', 0)
        th = bl.get('http_reqs', {})
        check('http_reqs', throughput, '>=',
              th.get('rate', 50), 'warning',
              'baseline_throughput')

        # business errors
        biz = metrics.get(f'{service}_errors', {}).get('values', {}).get('rate', 0)
        th = bl.get('business_errors', {})
        check(f'{service}_errors', biz, '<',
              th.get('rate', 0.05), bl_gate,
              'baseline_business_errors')
    else:
        print('\n--- Baseline Gate: results not available (SKIPPED) ---')
        summary['gates'].append({
            'gate': 'baseline', 'metric': 'present', 'actual': 'missing',
            'threshold': 'present', 'status': 'SKIP', 'severity': 'warning',
        })

    # ============================================================
    # Resilience / Chaos Gate
    # ============================================================
    recovery_path = os.path.join(args.artifacts, 'chaos-results',
                                 'chaos-recovery.json')
    if os.path.exists(recovery_path):
        print('\n--- Resilience Gate ---')
        recovery = json.load(open(recovery_path))
        experiments = (nfr.get('resilience', {})
                       .get('chaos_experiments', []))
        for exp in experiments:
            exp_name = exp.get('name', '')
            if exp_name.startswith(f'{service}-'):
                exp_name = exp_name[len(service) + 1:]
            raw_threshold = exp.get('recovery_threshold', '')
            if raw_threshold:
                if (isinstance(raw_threshold, str)
                        and raw_threshold.endswith('s')):
                    raw_threshold = raw_threshold[:-1]
                actual_sec = (recovery.get(exp_name, {})
                              .get('recovery_time_seconds'))
                if actual_sec is not None:
                    check(f'chaos.{exp_name}.recovery_time',
                          actual_sec, '<=', raw_threshold,
                          'critical', f'chaos_{exp_name}_recovery')

    # Chaos k6 results (smoke during chaos, informational)
    chaos_k6_path = os.path.join(args.artifacts, 'chaos-results',
                                 'chaos-results.json')
    chaos_k6 = load_k6_result(chaos_k6_path)
    if chaos_k6:
        c_metrics = chaos_k6.get('metrics', {})
        c_failed = c_metrics.get('http_req_failed', {}).get('values', {}).get('rate', 0)
        check('http_req_failed (during chaos)', c_failed, '<',
              0.05, 'warning', 'chaos_http_req_failed')

    # ============================================================
    # Sizing Drift Gate (CRITICAL)
    # Compares declared nfr.yaml sizing tier vs live Deployment resources.
    # Requires kubectl + cluster access. Skip with --skip-sizing-drift.
    # ============================================================
    if not args.skip_sizing_drift:
        check_sizing_drift(nfr, service, check, args.namespace)

    # ============================================================
    # Output
    # ============================================================
    out_path = os.path.join(args.artifacts, 'gate-summary.json')
    with open(out_path, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f'\n\u269B Gate Result: {summary["status"]}')
    print(f'Summary: {out_path}')

    return 1 if summary['status'] == 'FAILED' else 0


if __name__ == '__main__':
    sys.exit(main())
