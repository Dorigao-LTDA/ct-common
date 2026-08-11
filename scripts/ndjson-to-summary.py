#!/usr/bin/env python3
"""
Convert k6 --out json NDJSON output to --summary-export compatible JSON.

k6 v0.48+ removed --summary-export. The replacement is handleSummary() in JS,
but goja runtime limitations make it unreliable in some setups (wrappers, ESM).
This script post-processes the NDJSON from --out json to produce the aggregated
summary format that downstream tools (evaluate-gates.py, collect.py) expect.

Usage:
  python3 ndjson-to-summary.py --input raw.ndjson --output summary.json
"""
import argparse
import json
import math
import sys


def aggregate(lines):
    """Process NDJSON lines from k6 --out json, return metrics dict."""
    # Accumulators: metric_name -> {values, type}
    metrics = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get('type') == 'Metric':
            name = event.get('data', {}).get('name')
            mtype = event.get('data', {}).get('type')
            if name and mtype:
                if name not in metrics:
                    metrics[name] = {'type': mtype, 'samples': []}

        elif event.get('type') == 'Point':
            name = event.get('metric')
            value = event.get('data', {}).get('value')
            if name is not None and value is not None:
                if name not in metrics:
                    metrics[name] = {'type': 'unknown', 'samples': []}
                metrics[name]['samples'].append(value)

    # Compute aggregated values per metric type
    result = {}
    for name, mdata in metrics.items():
        mtype = mdata['type']
        samples = mdata['samples']
        if not samples:
            continue

        values = {}

        if mtype == 'counter':
            total = sum(samples)
            count = len(samples)
            values['count'] = total
            values['rate'] = round(total / count, 4) if count > 0 else 0

        elif mtype == 'trend':
            sorted_samples = sorted(samples)
            n = len(sorted_samples)
            values['avg'] = round(sum(samples) / n, 4)
            values['min'] = round(sorted_samples[0], 4)
            values['max'] = round(sorted_samples[-1], 4)
            values['med'] = round(percentile(sorted_samples, 0.50), 4)
            values['p(90)'] = round(percentile(sorted_samples, 0.90), 4)
            values['p(95)'] = round(percentile(sorted_samples, 0.95), 4)
            values['p(99)'] = round(percentile(sorted_samples, 0.99), 4)

        elif mtype == 'gauge':
            values['value'] = round(samples[-1], 4)

        elif mtype == 'rate':
            values['rate'] = round(sum(samples) / len(samples), 4) if samples else 0

        result[name] = {'values': values, 'type': mtype}

    return {'metrics': result} if result else None


def percentile(sorted_data, p):
    """Linear interpolation percentile."""
    if not sorted_data:
        return 0
    k = (len(sorted_data) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return d0 + d1


def main():
    parser = argparse.ArgumentParser(
        description='Convert k6 NDJSON to summary JSON'
    )
    parser.add_argument('--input', required=True, help='k6 --out json NDJSON file')
    parser.add_argument('--output', required=True, help='Output summary JSON file')
    args = parser.parse_args()

    try:
        with open(args.input) as f:
            lines = f.readlines()
    except OSError as e:
        print(f'ndjson-to-summary: cannot read input: {e}', file=sys.stderr)
        sys.exit(1)

    summary = aggregate(lines)
    if summary is None:
        print('ndjson-to-summary: no metrics found, writing empty summary', file=sys.stderr)
        summary = {'metrics': {}}

    with open(args.output, 'w') as f:
        json.dump(summary, f, indent=2)

    metric_count = len(summary.get('metrics', {}))
    size = len(json.dumps(summary))
    print(f'ndjson-to-summary: wrote {args.output} ({metric_count} metrics, {size} bytes)', file=sys.stderr)


if __name__ == '__main__':
    main()
