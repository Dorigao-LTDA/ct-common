#!/usr/bin/env python3
"""
Collect observability data from Mimir (PromQL), Loki (LogQL), and Tempo (traces)
during the test window. Designed to work with kubectl port-forward tunnels.

Usage:
  python3 o11y-collect.py \
    --mimir-url http://localhost:9090/prometheus \
    --loki-url http://localhost:3100 \
    --tempo-url http://localhost:3200 \
    --service catalogo \
    --test-window '2026-07-23T14:00:00Z/2026-07-23T14:10:00Z' \
    --output insights-input-o11y.json

Graceful failure: each query errors independently, never crashes.
If Mimir/Loki/Tempo unreachable → output with available=false.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# Mimir / PromQL queries — instant vector lookups
# ---------------------------------------------------------------------------
MIMIR_QUERIES = {
    'memory_usage_pct': (
        'container_memory_working_set_bytes'
        '{container="%(svc)s",namespace="app"}'
        ' / on(pod) kube_pod_container_resource_limits'
        '{resource="memory",container="%(svc)s",namespace="app"} * 100'
    ),
    'cpu_usage_pct': (
        'rate(container_cpu_usage_seconds_total'
        '{container="%(svc)s",namespace="app"}[5m])'
        ' / on(pod) kube_pod_container_resource_limits'
        '{resource="cpu",container="%(svc)s",namespace="app"} * 100'
    ),
    'jvm_heap_usage_pct': (
        'jvm_memory_used_bytes{area="heap",service_name="%(svc)s"}'
        ' / jvm_memory_max_bytes{area="heap",service_name="%(svc)s"} * 100'
    ),
    'jvm_gc_rate': (
        'rate(jvm_gc_pause_seconds_count{service_name="%(svc)s"}[5m])'
    ),
    'http_p95_seconds': (
        'histogram_quantile(0.95, '
        'rate(http_server_duration_seconds_bucket{service_name="%(svc)s"}[5m]))'
    ),
    'http_5xx_rate': (
        'rate(http_server_requests_seconds_count'
        '{service_name="%(svc)s",status=~"5.."}[5m])'
    ),
    'pod_restarts': (
        'kube_pod_container_status_restarts_total'
        '{container="%(svc)s",namespace="app"}'
    ),
}

# ---------------------------------------------------------------------------
# Loki / LogQL range queries
# ---------------------------------------------------------------------------
LOKI_QUERIES = (
    ('error_logs', '{service_name="%(svc)s"} |= "ERROR"'),
    ('oom_logs', '{service_name="%(svc)s"} |= "OutOfMemory" or "OOMKilled" or "exit code 137"'),
    ('connection_errors', '{service_name="%(svc)s"} |= "Connection refused" or "connection reset" or "timeout"'),
    ('warn_logs', '{service_name="%(svc)s"} |= "WARN"'),
)

# ---------------------------------------------------------------------------
# Tempo / trace search queries
# ---------------------------------------------------------------------------
TEMPO_QUERIES = (
    ('error_traces', {'status': 'error', 'limit': 5}),
    ('slow_traces', {'minDuration': '1s', 'limit': 5}),
)
# ponytail: Tempo's actual query API path varies by version.
# Default: /api/search (Tempo 2.x). If gateway wraps it, adjust base URL.


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------
def _fmt(svc, template):
    """Apply service name to a template string."""
    return template % {'svc': svc}


def _get_json(url, timeout=10):
    """GET a URL and return parsed JSON, or None on failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError,
            json.JSONDecodeError, OSError) as e:
        return {'_error': str(e)}


def _midpoint(ts_start, ts_end):
    """Return Unix timestamp at the midpoint of a window."""
    # Accept both ISO8601 and Unix timestamps
    for ts in (ts_start, ts_end):
        if ts is None:
            return None
    # ponytail: assume ISO8601 with Z suffix. Parsing leniently with strptime.
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%s'):
        try:
            start_sec = time.mktime(time.strptime(ts_start, fmt))
            end_sec = time.mktime(time.strptime(ts_end, fmt))
            return int((start_sec + end_sec) / 2)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------
def _collect_mimir(mimir_url, service, midpoint_ts):
    """Run all PromQL queries against Mimir and return results dict."""
    base = mimir_url.rstrip('/')
    result = {'available': True, 'queries': {}}

    for name, query_tpl in MIMIR_QUERIES.items():
        query = _fmt(service, query_tpl)
        params = f'query={urllib.parse.quote(query)}&time={midpoint_ts}'
        url = f'{base}/api/v1/query?{params}'

        resp = _get_json(url)
        if resp is None:
            result['queries'][name] = None
            continue
        if '_error' in resp:
            result['queries'][name] = resp['_error']
            continue

        # Extract scalar value from Prometheus response
        data = resp.get('data', {})
        vec = data.get('result', [])
        if vec and 'value' in vec[0] and len(vec[0]['value']) > 1:
            result['queries'][name] = float(vec[0]['value'][1])
        else:
            result['queries'][name] = 0.0  # empty vector → zero

    return result


def _collect_loki(loki_url, service, start_iso, end_iso):
    """Run LogQL range queries and return sample lines."""
    base = loki_url.rstrip('/')
    result = {'available': True, 'queries': {}}

    for name, query_tpl in LOKI_QUERIES:
        query = _fmt(service, query_tpl)
        params = (
            f'query={urllib.parse.quote(query)}'
            f'&start={urllib.parse.quote(start_iso)}'
            f'&end={urllib.parse.quote(end_iso)}'
            '&limit=100&direction=backward'
        )
        url = f'{base}/loki/api/v1/query_range?{params}'

        resp = _get_json(url)
        if resp is None:
            result['queries'][name] = {'count': 0, 'samples': []}
            continue
        if '_error' in resp:
            result['queries'][name] = {'error': resp['_error'], 'count': 0}
            continue

        streams = resp.get('data', {}).get('result', [])
        samples = []
        for stream in streams:
            for entry in stream.get('values', [])[:3]:
                line = entry[1] if len(entry) > 1 else ''
                samples.append(line[:300])  # truncate
        result['queries'][name] = {
            'count': len(samples),
            'samples': samples,
        }

    return result


def _collect_tempo(tempo_url, service):
    """Search traces by service name with various filters."""
    base = tempo_url.rstrip('/')
    result = {'available': True, 'queries': {}}

    for name, params in TEMPO_QUERIES:
        qs = f'serviceName={urllib.parse.quote(service)}'
        for k, v in params.items():
            qs += f'&{k}={urllib.parse.quote(str(v))}'
        url = f'{base}/api/search?{qs}'

        resp = _get_json(url, timeout=15)
        if resp is None:
            result['queries'][name] = {'count': 0, 'trace_ids': []}
            continue
        if '_error' in resp:
            result['queries'][name] = {'error': resp['_error'], 'count': 0}
            continue

        traces = resp.get('traces', []) if isinstance(resp, dict) else []
        ids = []
        for t in traces[:5]:
            tid = t if isinstance(t, str) else t.get('traceID', '')
            if tid:
                ids.append(tid)
        result['queries'][name] = {
            'count': len(traces),
            'trace_ids': ids,
        }

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Collect observability data from Mimir/Loki/Tempo'
    )
    parser.add_argument('--mimir-url', help='Mimir PromQL base URL (with /prometheus or /)')
    parser.add_argument('--loki-url', help='Loki LogQL base URL')
    parser.add_argument('--tempo-url', help='Tempo query API base URL')
    parser.add_argument('--service', required=True, help='Service name (catalogo, pagamento, pedido)')
    parser.add_argument('--test-window', required=True,
                        help='ISO8601 window: start/end (e.g. 2026-07-23T14:00:00Z/2026-07-23T14:10:00Z)')
    parser.add_argument('--output', default='insights-input-o11y.json',
                        help='Output file path')
    parser.add_argument('--skip-o11y', action='store_true',
                        help='Skip all queries, write stub')
    args = parser.parse_args()

    result = {
        'available': False,
        'service': args.service,
        'query_time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'test_window': args.test_window,
    }

    if args.skip_o11y:
        result['reason'] = '--skip-o11y flag set'
        with open(args.output, 'w') as f:
            json.dump(result, f, indent=2)
        print(f'o11y-collect: skipped (flag), wrote {args.output}', file=sys.stderr)
        return

    # Parse window
    parts = args.test_window.split('/')
    if len(parts) != 2:
        sys.exit(f'ERROR: --test-window must be start/end, got: {args.test_window}')
    start_iso, end_iso = parts[0], parts[1]
    midpoint_ts = _midpoint(start_iso, end_iso)
    if midpoint_ts is None:
        print('o11y-collect: WARNING could not parse test window timestamps',
              file=sys.stderr)

    # Collect from each source
    if args.mimir_url:
        try:
            result['mimir'] = _collect_mimir(args.mimir_url, args.service, midpoint_ts)
            result['available'] = True
        except Exception as e:
            result['mimir'] = {'available': False, 'error': str(e)}
            print(f'o11y-collect: Mimir error: {e}', file=sys.stderr)

    if args.loki_url:
        try:
            result['loki'] = _collect_loki(args.loki_url, args.service, start_iso, end_iso)
            result['available'] = True
        except Exception as e:
            result['loki'] = {'available': False, 'error': str(e)}
            print(f'o11y-collect: Loki error: {e}', file=sys.stderr)

    if args.tempo_url:
        try:
            result['tempo'] = _collect_tempo(args.tempo_url, args.service)
            result['available'] = True
        except Exception as e:
            result['tempo'] = {'available': False, 'error': str(e)}
            print(f'o11y-collect: Tempo error: {e}', file=sys.stderr)

    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2)
    print(f'o11y-collect: wrote {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
