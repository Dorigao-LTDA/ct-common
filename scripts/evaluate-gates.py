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


def main():
    parser = argparse.ArgumentParser(
        description='Evaluate NFR gates against test results'
    )
    parser.add_argument('--artifacts', required=True,
                        help='Directory with test artifacts')
    parser.add_argument('--nfr', required=True, help='Path to nfr.yaml')
    parser.add_argument('--service', required=True,
                        help='Service name (catalogo, pagamento, pedido)')
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
        bl = (nfr.get('performance', {}).get('http', {})
              .get('thresholds', {}))

        # http_req_failed
        failed_rate = metrics.get('http_req_failed', {}).get('values', {}).get('rate', 0)
        th = bl.get('http_req_failed', {})
        check('http_req_failed', failed_rate, '<',
              th.get('rate', 0.01), th.get('gate', 'critical'),
              'baseline_http_req_failed')

        # http_req_duration p95
        p95 = metrics.get('http_req_duration', {}).get('values', {}).get('p(95)', 0)
        th = bl.get('http_req_duration', {})
        check('http_req_duration.p95', p95, '<',
              th.get('p95', 300), th.get('gate', 'critical'),
              'baseline_p95')

        # http_req_duration p99
        p99 = metrics.get('http_req_duration', {}).get('values', {}).get('p(99)', 0)
        th = bl.get('http_req_duration', {})
        check('http_req_duration.p99', p99, '<',
              th.get('p99', 800), th.get('gate', 'critical'),
              'baseline_p99')

        # throughput
        throughput = metrics.get('http_reqs', {}).get('values', {}).get('rate', 0)
        th = bl.get('throughput', {})
        check('http_reqs', throughput, '>=',
              th.get('min', 50), th.get('gate', 'warning'),
              'baseline_throughput')

        # business errors
        biz = metrics.get(f'{service}_errors', {}).get('values', {}).get('rate', 0)
        th = bl.get('business_errors', {})
        check(f'{service}_errors', biz, '<',
              th.get('rate', 0.05), th.get('gate', 'critical'),
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
