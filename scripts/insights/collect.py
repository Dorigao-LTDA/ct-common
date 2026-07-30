#!/usr/bin/env python3
"""
Aggregate all test artifacts + optional observability/code inputs into
a single structured JSON consumed by the LLM analyzer.

Usage:
  python3 collect.py \
    --artifacts-dir . \
    --nfr nfr.yaml \
    --service catalogo \
    --commit abc1234 \
    --o11y-input insights-input-o11y.json \
    --code-input insights-input-code.json \
    --output insights-input.json
"""
import argparse
import json
import os
import sys
import time


def parse_yaml(path):
    """PyYAML is pre-installed on ubuntu-24.04 runners."""
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def load_k6_result(path):
    """Read k6 --summary-export JSON or NDJSON, return metrics or None."""
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        content = f.read().strip()
    if not content:
        return None

    # Try single JSON format
    try:
        data = json.loads(content)
        if isinstance(data, dict) and 'metrics' in data:
            result = {}
            for name, md in data['metrics'].items():
                if isinstance(md, dict) and 'values' in md:
                    result[name] = {'values': md['values']}
            if result:
                return {'metrics': result}
    except json.JSONDecodeError:
        pass

    # Fallback NDJSON format
    metrics = {}
    for line in content.split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get('type') != 'Metric':
            continue
        name = ev.get('metric')
        values = ev.get('data', {}).get('values')
        if name and values:
            metrics.setdefault(name, {'values': {}})
            metrics[name]['values'].update(values)
    return {'metrics': metrics} if metrics else None


def find_artifact(artifacts_dir, name):
    """Resolve artifact file considering download-artifact subdirectory structure."""
    for candidate in [
        os.path.join(artifacts_dir, name),
        os.path.join(artifacts_dir, name.replace('.json', ''), name),
        os.path.join(artifacts_dir, 'perf-results', name),
    ]:
        if os.path.exists(candidate):
            return candidate
    return None


def extract_k6_summary(data):
    """Flatten key k6 metric scalars into a flat dict."""
    if not data or 'metrics' not in data:
        return {}
    m = data['metrics']
    result = {}
    keys = [
        ('http_req_failed', 'rate'),
        ('http_req_duration', 'p(95)'),
        ('http_req_duration', 'p(99)'),
        ('http_req_duration', 'avg'),
        ('http_reqs', 'rate'),
        ('http_req_waiting', 'avg'),
        ('http_req_connecting', 'avg'),
        ('iterations', 'rate'),
        ('data_sent', 'rate'),
        ('data_received', 'rate'),
        ('vus', 'value'),
        ('vus_max', 'value'),
    ]
    for metric, key in keys:
        val = m.get(metric, {}).get('values', {}).get(key)
        if val is not None:
            label = f'{metric}.{key}' if key != 'value' else metric
            result[label] = round(val, 4)
    return result


def extract_custom_metrics(data, service):
    """Extract custom Rate and Trend metrics (e.g. catalogo_errors)."""
    if not data or 'metrics' not in data:
        return {}
    m = data['metrics']
    result = {}
    # Try service name as-is and short name
    for prefix in [service, service.split('-')[-1]]:
        for suffix in ['errors', 'list_duration', 'get_duration',
                        'create_duration', 'health_duration']:
            name = f'{prefix}_{suffix}'
            vals = m.get(name, {}).get('values', {})
            if 'rate' in vals:
                result[f'{name}.rate'] = round(vals['rate'], 4)
            if 'avg' in vals:
                result[f'{name}.avg'] = round(vals['avg'], 2)
    return result


def main():
    parser = argparse.ArgumentParser(
        description='Collect and aggregate test data for LLM analysis'
    )
    parser.add_argument('--artifacts-dir', required=True,
                        help='Directory with k6/chaos/gate JSONs')
    parser.add_argument('--nfr', required=True,
                        help='Path to nfr.yaml')
    parser.add_argument('--service', required=True,
                        help='Service name')
    parser.add_argument('--commit', required=True,
                        help='Git commit SHA')
    parser.add_argument('--o11y-input',
                        help='Path to o11y-collect.py output')
    parser.add_argument('--code-input',
                        help='Path to code-analyze.py output')
    parser.add_argument('--output', default='insights-input.json',
                        help='Output file')
    args = parser.parse_args()

    # --- NFR ---
    nfr = parse_yaml(args.nfr)

    # --- Gate summary ---
    gate_data = {}
    gp = find_artifact(args.artifacts_dir, 'gate-summary.json')
    if gp:
        try:
            gate_data = json.load(open(gp))
        except (json.JSONDecodeError, OSError):
            pass

    # --- k6 results per scenario ---
    test_results = {}
    for sc in ['smoke', 'baseline', 'stress', 'spike']:
        fp = find_artifact(args.artifacts_dir, f'{sc}-results.json')
        if fp:
            data = load_k6_result(fp)
            if data:
                test_results[sc] = {
                    'k6_summary': extract_k6_summary(data),
                    'custom_metrics': extract_custom_metrics(data, args.service),
                }

    # --- Chaos results ---
    chaos = {}
    cr = find_artifact(args.artifacts_dir, 'chaos-recovery.json')
    if cr:
        try:
            chaos['recovery'] = json.load(open(cr))
        except (json.JSONDecodeError, OSError):
            pass
    ck = find_artifact(args.artifacts_dir, 'chaos-results.json')
    if ck:
        data = load_k6_result(ck)
        if data:
            chaos['k6_during_chaos'] = extract_k6_summary(data)
    test_results['chaos'] = chaos

    # --- Optional inputs ---
    o11y_data = {}
    if args.o11y_input and os.path.exists(args.o11y_input):
        try:
            o11y_data = json.load(open(args.o11y_input))
        except (json.JSONDecodeError, OSError):
            o11y_data = {'available': False, 'error': 'parse failure'}

    code_data = {}
    if args.code_input and os.path.exists(args.code_input):
        try:
            code_data = json.load(open(args.code_input))
        except (json.JSONDecodeError, OSError):
            code_data = {'available': False, 'error': 'parse failure'}

    # --- Thresholds (lean version for LLM) ---
    perf_thresholds = {}
    for sc_name, sc_cfg in nfr.get('performance', {}).get('scenarios', {}).items():
        perf_thresholds[sc_name] = sc_cfg.get('thresholds', {})

    resilience_cfg = {
        'experiments': nfr.get('resilience', {}).get('chaos_experiments', []),
        'sla': nfr.get('resilience', {}).get('sla', {}),
    }

    # --- Assemble ---
    output = {
        'meta': {
            'service': args.service,
            'commit': args.commit,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'gate_status': gate_data.get('status', 'UNKNOWN'),
        },
        'test_results': test_results,
        'thresholds': {
            'performance': perf_thresholds,
            'resilience': resilience_cfg,
            'resources': nfr.get('resources', {}),
        },
        'gates': gate_data.get('gates', []),
        'observability': o11y_data,
        'code_changes': code_data,
    }

    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)
    size = len(json.dumps(output))
    print(f'collect: wrote {args.output} ({size} bytes)', file=sys.stderr)


if __name__ == '__main__':
    main()
