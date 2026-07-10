#!/usr/bin/env python3
"""
Parse nfr.yaml and generate shell environment variables for k6 scripts.
Makes nfr.yaml the single source of truth for thresholds and scenario parameters.

Usage: python3 scripts/nfr-to-env.py --nfr nfr.yaml --output nfr-env.sh --service <name>
Pipeline sources nfr-env.sh before k6 executions.
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


def main():
    parser = argparse.ArgumentParser(
        description='Generate k6 env vars from nfr.yaml'
    )
    parser.add_argument('--nfr', required=True, help='Path to nfr.yaml')
    parser.add_argument('--output', required=True, help='Output env file')
    parser.add_argument('--service', required=True, help='Service name')
    args = parser.parse_args()

    nfr = parse_yaml(args.nfr)
    perf = nfr.get('performance', {})
    http = perf.get('http', {})
    global_thresholds = http.get('thresholds', {})
    scenarios = perf.get('scenarios', {})
    lines = []

    # --- Business errors threshold (global) ---
    biz_errors = (
        global_thresholds.get('business_errors', {}).get('rate', 0.05)
    )
    lines.append(f'export K6_BUSINESS_ERRORS_THRESHOLD={biz_errors}')

    # --- Per-scenario thresholds and parameters ---
    for name, scenario in scenarios.items():
        prefix = f'K6_{name.upper()}'
        executor = scenario.get('executor', '')
        vus = scenario.get('vus')
        duration = scenario.get('duration')
        stages = scenario.get('stages')
        sc_thresholds = scenario.get('thresholds', {})

        if executor:
            lines.append(f'export {prefix}_EXECUTOR={executor}')
        if vus is not None:
            lines.append(f'export {prefix}_VUS={vus}')
        if duration:
            lines.append(f'export {prefix}_DURATION={duration}')
        if stages:
            lines.append(
                f'export {prefix}_STAGES={json.dumps(stages, separators=(",", ":"))}'
            )

        # Scenario-level thresholds
        for metric, th in sc_thresholds.items():
            if metric == 'gate':
                lines.append(f'export {prefix}_GATE={th}')
            elif isinstance(th, dict):
                for key, val in th.items():
                    lines.append(
                        f'export {prefix}_THRESHOLD_{metric.upper()}_{key.upper()}={val}'
                    )
            else:
                lines.append(
                    f'export {prefix}_THRESHOLD_{metric.upper()}={th}'
                )

    # --- Chaos recovery thresholds ---
    resilience = nfr.get('resilience', {})
    for exp in resilience.get('chaos_experiments', []):
        exp_name = exp.get('name', '')
        # Strip service prefix: "catalogo-pod-kill" → "pod-kill"
        if exp_name.startswith(f'{args.service}-'):
            exp_name = exp_name[len(args.service) + 1:]
        exp_key = exp_name.upper().replace('-', '_')
        recovery = exp.get('recovery_threshold', '')
        if recovery:
            # Strip trailing 's' if present (e.g. "30s" → "30")
            if isinstance(recovery, str) and recovery.endswith('s'):
                recovery = recovery[:-1]
            lines.append(
                f'export K6_CHAOS_{exp_key}_RECOVERY_THRESHOLD={recovery}'
            )

    # Write output
    with open(args.output, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    print(f'nfr-to-env: wrote {len(lines)} vars to {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
