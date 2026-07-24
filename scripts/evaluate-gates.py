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
    """
    Parse k6 output into a single metrics dict.

    Accepts two formats:
      1. k6 --summary-export output (single JSON object with "metrics" key).
      2. k6 --out json output (NDJSON, one JSON line per event).

    Returns {metrics: {<name>: {values: {...}}}} or None when the file is
    missing, empty, or contains no parsable metrics.
    """
    if not os.path.exists(path):
        return None

    with open(path) as f:
        content = f.read().strip()

    if not content:
        return None

    # --- Try --summary-export format first (single JSON) ---
    try:
        data = json.loads(content)
        if isinstance(data, dict) and 'metrics' in data:
            result = {}
            for name, metric_data in data['metrics'].items():
                if isinstance(metric_data, dict) and 'values' in metric_data:
                    result[name] = {'values': metric_data['values']}
            if result:
                return {'metrics': result}
    except json.JSONDecodeError:
        pass

    # --- Fallback: NDJSON format (--out json) ---
    metrics = {}
    for line in content.split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get('type') != 'Metric':
            continue
        name = event.get('metric')
        values = event.get('data', {}).get('values')
        if name and values:
            if name not in metrics:
                metrics[name] = {'values': {}}
            metrics[name]['values'].update(values)

    return {'metrics': metrics} if metrics else None


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

    def find_result(name):
        """Look for result file directly in artifacts dir or in a subdirectory."""
        # Try direct path first (inline workflow: file in workspace root)
        direct = os.path.join(args.artifacts, name)
        result = load_k6_result(direct)
        if result is not None:
            return result
        # Try subdirectory named after the artifact (download-artifact workflow)
        subdir = os.path.join(args.artifacts, name.replace('.json', ''), name)
        result = load_k6_result(subdir)
        if result is not None:
            return result
        # Try perf-results subdirectory (legacy upload-artifact naming)
        legacy = os.path.join(args.artifacts, 'perf-results', name)
        return load_k6_result(legacy)

    def evaluate_scenario(scenario_name, severity_override=None):
        """Evaluate a single test scenario gate from its k6 result file."""
        result_file = f'{scenario_name}-results.json'
        data = find_result(result_file)
        if data is None:
            print(f'\n--- {scenario_name.title()} Gate: results not available ---')
            summary['gates'].append({
                'gate': scenario_name, 'metric': 'present', 'actual': 'missing',
                'threshold': 'present', 'status': 'SKIP', 'severity': 'warning',
            })
            return

        metrics = data.get('metrics', {})
        sc_cfg = (nfr.get('performance', {}).get('scenarios', {})
                  .get(scenario_name, {}).get('thresholds', {}))
        severity = sc_cfg.get('gate', severity_override or 'critical') if isinstance(sc_cfg, dict) else (severity_override or 'critical')
        gate_prefix = f'{scenario_name}_'

        print(f'\n--- {scenario_name.title()} Gate (severity={severity}) ---')

        # http_req_failed
        failed_rate = metrics.get('http_req_failed', {}).get('values', {}).get('rate', 0)
        th = sc_cfg.get('http_req_failed', {}) if isinstance(sc_cfg, dict) else {}
        check('http_req_failed', failed_rate, '<',
              th.get('rate', 0.01) if scenario_name == 'baseline' else th.get('rate', 0.05),
              severity, f'{gate_prefix}http_req_failed')

        # http_req_duration p95
        p95 = metrics.get('http_req_duration', {}).get('values', {}).get('p(95)', 0)
        th = sc_cfg.get('http_req_duration', {}) if isinstance(sc_cfg, dict) else {}
        p95_threshold = th.get('p95', 300) if scenario_name == 'baseline' else th.get('p95', 2000)
        check('http_req_duration.p95', p95, '<',
              p95_threshold, severity, f'{gate_prefix}p95')

        # http_req_duration p99
        p99 = metrics.get('http_req_duration', {}).get('values', {}).get('p(99)', 0)
        p99_threshold = th.get('p99', 800) if scenario_name == 'baseline' else th.get('p99', 3000)
        check('http_req_duration.p99', p99, '<',
              p99_threshold, severity, f'{gate_prefix}p99')

        # Throughput (only for baseline)
        if scenario_name == 'baseline':
            throughput = metrics.get('http_reqs', {}).get('values', {}).get('rate', 0)
            th = sc_cfg.get('http_reqs', {})
            check('http_reqs', throughput, '>=',
                  th.get('rate', 10), 'warning',
                  'baseline_throughput')

        # Business errors (only for baseline)
        if scenario_name == 'baseline':
            biz = metrics.get(f'{service}_errors', {}).get('values', {}).get('rate', 0)
            th = sc_cfg.get('business_errors', {})
            check(f'{service}_errors', biz, '<',
                  th.get('rate', 0.05), severity,
                  'baseline_business_errors')

        # Log k6's own threshold summary for reference
        thresholds_metric = metrics.get('thresholds', {})
        if thresholds_metric:
            for metric_name, status in thresholds_metric.items():
                print(f'  (k6) {metric_name}: {status}')

    # ============================================================
    # Evaluate all test scenarios
    # ============================================================
    for sc in ['smoke', 'baseline', 'stress', 'spike']:
        evaluate_scenario(sc)

    # ============================================================
    # Resilience / Chaos Gate
    # ============================================================
    def find_json(name):
        """Look for a JSON file in artifacts dir or common subdirectories."""
        for candidate in [
            os.path.join(args.artifacts, name),
            os.path.join(args.artifacts, 'chaos-results', name),
        ]:
            if os.path.exists(candidate):
                return candidate
        return None

    recovery_path = find_json('chaos-recovery.json')
    if recovery_path:
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
    chaos_k6_path = find_json('chaos-results.json')
    if chaos_k6_path:
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
