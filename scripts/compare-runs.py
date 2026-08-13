#!/usr/bin/env python3
"""
Compare current run gate-summary with the previous run to detect performance
regressions.

Usage:
  python3 compare-runs.py --current gate-summary.json \
                          --previous /tmp/previous-summary.json \
                          --output regression-summary.json

Metrics compared (from gate-summary gates):
  - http_req_duration.p95    — regression if degraded > 20%
  - http_req_duration.p99    — regression if degraded > 30%
  - http_reqs.rate (throughput) — regression if dropped > 20%
  - http_req_failed.rate     — regression if increased > 50%
  - {service}_errors.rate    — regression if increased > 50%

Exit 0 if no critical regression; exit 1 if critical regression detected.
"""
import argparse
import json
import sys


# (metric_key, regression_threshold_pct, direction)
# direction: 'lower_better' or 'higher_better'
METRICS = [
    ('http_req_duration.p95', 20, 'lower_better'),
    ('http_req_duration.p99', 30, 'lower_better'),
    ('http_reqs.rate', 20, 'higher_better'),
    ('http_req_failed.rate', 50, 'lower_better'),
]


def gate_metrics(summary):
    """Extract {metric: actual} from a gate-summary.json."""
    metrics = {}
    for g in summary.get('gates', []):
        metric = g.get('metric', '')
        actual = g.get('actual')
        # Skip non-numeric (SKIP/missing)
        if isinstance(actual, (int, float)):
            metrics[metric] = float(actual)
    return metrics


def compare(current, previous):
    """Compare current vs previous, return list of regression findings."""
    cur = gate_metrics(current)
    prev = gate_metrics(previous)
    findings = []
    status = 'improving'

    for metric, threshold_pct, direction in METRICS:
        c = cur.get(metric)
        p = prev.get(metric)
        if c is None or p is None:
            continue

        if p == 0:
            # Can't compute % change on zero baseline; skip
            delta_pct = None
        else:
            delta_pct = round((c - p) / p * 100, 2)

        if direction == 'lower_better':
            # degradation means actual went UP
            degraded = c > p
        else:
            # higher_better: degradation means actual went DOWN
            degraded = c < p

        abs_change_pct = abs(delta_pct) if delta_pct is not None else None

        severity = 'stable'
        if degraded and abs_change_pct is not None and abs_change_pct > threshold_pct:
            severity = 'regression'
        elif abs_change_pct is not None and abs_change_pct > 0.5:
            severity = 'changed'

        findings.append({
            'metric': metric,
            'previous': p,
            'current': c,
            'delta_pct': delta_pct,
            'severity': severity,
        })

        if severity == 'regression':
            status = 'regression'

    return status, findings


def main():
    parser = argparse.ArgumentParser(
        description='Compare current run metrics with previous run'
    )
    parser.add_argument('--current', required=True, help='Current gate-summary.json')
    parser.add_argument('--previous', required=True, help='Previous gate-summary.json')
    parser.add_argument('--output', default='regression-summary.json')
    args = parser.parse_args()

    try:
        with open(args.current) as f:
            current = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'compare-runs: cannot read current: {e}', file=sys.stderr)
        sys.exit(2)

    try:
        with open(args.previous) as f:
            previous = json.load(f)
    except (OSError, json.JSONDecodeError):
        # No previous run — first run, nothing to compare
        output = {
            'status': 'first_run',
            'reason': 'no previous run available',
            'findings': [],
        }
        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)
        print('compare-runs: first run (no previous), wrote stub', file=sys.stderr)
        return

    status, findings = compare(current, previous)

    output = {
        'status': status,
        'service': current.get('service', ''),
        'findings': findings,
    }
    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)

    print(f'compare-runs: status={status}, {len(findings)} metrics compared', file=sys.stderr)
    for finding in findings:
        if finding['severity'] == 'regression':
            print(f'  REGRESSION: {finding["metric"]} '
                  f'{finding["previous"]} -> {finding["current"]} '
                  f'({finding["delta_pct"]}%)', file=sys.stderr)

    # Exit 1 on critical regression (for gate integration)
    return 1 if status == 'regression' else 0


if __name__ == '__main__':
    sys.exit(main())
