#!/usr/bin/env python3
"""
Generate a developer-facing insights report (Markdown + JSON) from test data
and LLM analysis results.

Usage:
  python3 report.py --test-data insights-input.json \
                    --llm-response insights-llm-response.json \
                    --output insights-report.md

Outputs:
  - <output>.md      (Markdown report for developer reading)
  - <output>.json    (machine-readable, test data + LLM analysis combined)
"""
import argparse
import json
import os
import sys


def _icon(gate_status):
    return '✅' if gate_status == 'PASS' else ('⏭️' if gate_status == 'SKIP' else '❌')


def _severity_label(s):
    return f'🔴 {s.upper()}' if s == 'critical' else (f'🟡 {s.title()}' if s == 'warning' else f'🔵 {s.title()}')


def build_markdown(test_data, llm_response):
    """Assemble the developer Markdown report."""
    meta = test_data.get('meta', {})
    gates = test_data.get('gates', [])
    findings = llm_response.get('findings', [])
    results = test_data.get('test_results', {})

    lines = []
    gate_icon = '✅' if meta.get('gate_status') == 'PASSED' else '🚫'

    # Header
    lines.append(f'# {gate_icon} Pipeline Insights — `{meta.get("service", "?")}`')
    lines.append('')
    lines.append(f'| | |')
    lines.append(f'|---|---|')
    lines.append(f'| **Commit** | `{meta.get("commit", "N/A")}` |')
    lines.append(f'| **Timestamp** | {meta.get("timestamp", "N/A")} |')
    lines.append(f'| **Gate Status** | {gate_icon} {meta.get("gate_status", "UNKNOWN")} |')
    lines.append('')

    # --- Gate Summary ---
    lines.append('## 📋 Gate Summary')
    lines.append('')
    if gates:
        lines.append('| Gate | Metric | Actual | Threshold | Status | Severity |')
        lines.append('|------|--------|--------|-----------|--------|----------|')
        for g in gates:
            lines.append(
                f'| {_icon(g.get("status"))} {g.get("gate")} | {g.get("metric")} | '
                f'{g.get("actual")} | {g.get("threshold")} | '
                f'{g.get("status")} | {g.get("severity")} |'
            )
    else:
        lines.append('_No gates evaluated._')
    lines.append('')

    # --- Performance Metrics ---
    lines.append('## 📊 Performance Metrics')
    lines.append('')
    has_perf = False
    for sc in ['smoke', 'baseline', 'stress', 'spike']:
        sc_data = results.get(sc, {})
        if not sc_data:
            continue
        has_perf = True
        lines.append(f'### {sc.title()}')
        lines.append('')
        k6 = sc_data.get('k6_summary', {})
        if k6:
            lines.append('| Metric | Value |')
            lines.append('|--------|-------|')
            for k, v in sorted(k6.items()):
                lines.append(f'| `{k}` | {v} |')
            lines.append('')
        custom = sc_data.get('custom_metrics', {})
        if custom:
            lines.append('| Custom Metric | Value |')
            lines.append('|---------------|-------|')
            for k, v in sorted(custom.items()):
                lines.append(f'| `{k}` | {v} |')
            lines.append('')
    if not has_perf:
        lines.append('_No performance test data available._')
        lines.append('')

    # --- Chaos / Resilience ---
    chaos = results.get('chaos', {})
    recovery = chaos.get('recovery', {})
    if recovery:
        lines.append('## 🔄 Resilience (Chaos)')
        lines.append('')
        lines.append('| Experiment | Recovery (s) |')
        lines.append('|------------|--------------|')
        for exp_name, exp_data in recovery.items():
            sec = exp_data.get('recovery_time_seconds', 'N/A')
            lines.append(f'| {exp_name} | {sec}s |')
        lines.append('')

    # --- Observability Snapshot ---
    o11y = test_data.get('observability', {})
    if o11y.get('available'):
        lines.append('## 🔭 Observability Snapshot')
        lines.append('')
        lines.append(f'_Collected during test window: {o11y.get("test_window", "N/A")}_')
        lines.append('')

        # Mimir
        mimir = o11y.get('mimir', {})
        if mimir.get('available'):
            lines.append('### Metrics (Mimir)')
            lines.append('| Query | Value |')
            lines.append('|-------|-------|')
            for qname, qval in mimir.get('queries', {}).items():
                if qval is None:
                    continue
                if isinstance(qval, str):  # error
                    lines.append(f'| {qname} | ❌ {qval} |')
                else:
                    lines.append(f'| {qname} | {qval} |')
            lines.append('')

        # Loki
        loki = o11y.get('loki', {})
        if loki.get('available'):
            for lname, ldata in loki.get('queries', {}).items():
                if isinstance(ldata, dict) and 'count' in ldata:
                    count = ldata['count']
                    lines.append(f'| `{lname}` | {count} matches |')
                elif isinstance(ldata, dict) and 'error' in ldata:
                    lines.append(f'| `{lname}` | ❌ {ldata["error"]} |')
            lines.append('')

        # Tempo
        tempo = o11y.get('tempo', {})
        if tempo.get('available'):
            lines.append('### Traces (Tempo)')
            for tname, tdata in tempo.get('queries', {}).items():
                if isinstance(tdata, dict) and 'count' in tdata:
                    lines.append(f'| `{tname}` | {tdata["count"]} traces |')
                    if tdata.get('trace_ids'):
                        lines.append(f'| IDs | `{", ".join(tdata["trace_ids"][:3])}` |')
                elif isinstance(tdata, dict) and 'error' in tdata:
                    lines.append(f'| `{tname}` | ❌ {tdata["error"]} |')
            lines.append('')
    else:
        lines.append('## 🔭 Observability Snapshot')
        lines.append('')
        lines.append('_Not available — runner had no cluster access or services unreachable._')
        lines.append('')

    # --- Code Changes ---
    code = test_data.get('code_changes', {})
    if code.get('available'):
        lines.append('## 📝 Code Changes Analyzed')
        lines.append('')
        lines.append(f'_Diff summary: {code.get("diff_summary", "N/A")}_')
        lines.append('')
        changed = code.get('changed_files', [])
        if changed:
            lines.append(f'**{len(changed)} files changed:**')
            for f in changed:
                lines.append(f'- `{f}`')
            lines.append('')
        endpoints = code.get('changed_endpoints', [])
        if endpoints:
            lines.append('**Detected endpoint changes:**')
            for ep in endpoints:
                lines.append(f'- `{ep["annotation"]} {ep["path"]}` in `{ep["file"]}`')
            lines.append('')
        deps = code.get('dependency_changes', [])
        if deps:
            lines.append('**Dependency changes:**')
            for d in deps:
                lines.append(f'- `{d.get("artifact")}` → v{d.get("version")}')
            lines.append('')
        rc = code.get('resource_changes', {})
        if rc:
            lines.append('**Resource config changes:**')
            for k, v in rc.items():
                lines.append(f'- `{k}`: {v.get("from")} → {v.get("to")}')
            lines.append('')
    else:
        lines.append('## 📝 Code Changes')
        lines.append('')
        lines.append('_Not available — shallow clone or base ref inaccessible._')
        lines.append('')

    # --- LLM Analysis ---
    lines.append('## 🤖 LLM Analysis')
    lines.append('')

    if llm_response.get('errors'):
        lines.append('> ⚠️ Some LLM calls had errors:')
        for name, err in llm_response['errors'].items():
            lines.append(f'> - **{name}**: {err}')
        lines.append('')

    if not findings:
        lines.append('_No findings from LLM analysis._')
        lines.append('')
    else:
        for severity in ['critical', 'warning', 'info']:
            items = [f for f in findings if f.get('severity') == severity]
            if not items:
                continue
            lines.append(f'### {_severity_label(severity)}')
            lines.append('')
            for item in items:
                lines.append(f'**{item.get("title", "Finding")}**')
                lines.append('')
                desc = item.get('description', item.get('hypothesis', ''))
                if desc:
                    lines.append(f'{desc}')
                    lines.append('')
                rec = item.get('recommendation', '')
                if rec:
                    lines.append(f'> **Recommendation:** {rec}')
                    lines.append('')
                # Show metric details if present
                metric = item.get('metric', '')
                actual = item.get('actual', '')
                threshold = item.get('threshold', '')
                if metric and actual and threshold:
                    lines.append(f'> Metric: `{metric}` | Actual: `{actual}` | Threshold: `{threshold}`')
                    lines.append('')

    # Executive Summary
    exec_summary = llm_response.get('executive_summary', '')
    if exec_summary:
        lines.append('## 🎯 Executive Summary')
        lines.append('')
        lines.append(exec_summary)
        lines.append('')

    # Footer
    lines.append('---')
    lines.append(f'_Report generated by ct-common/scripts/insights | {meta.get("timestamp", "")}_')
    lines.append('')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate insights report')
    parser.add_argument('--test-data', required=True,
                        help='Path to insights-input.json')
    parser.add_argument('--llm-response', required=True,
                        help='Path to insights-llm-response.json')
    parser.add_argument('--output', default='insights-report.md',
                        help='Output Markdown file')
    args = parser.parse_args()

    # Load test data
    try:
        with open(args.test_data) as f:
            test_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'report: ERROR loading {args.test_data}: {e}', file=sys.stderr)
        sys.exit(1)

    # Load LLM response (optional — file may not exist if analyze.py was skipped)
    llm_response = {}
    if os.path.exists(args.llm_response):
        try:
            with open(args.llm_response) as f:
                llm_response = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Generate Markdown
    md = build_markdown(test_data, llm_response)
    with open(args.output, 'w') as f:
        f.write(md)
    print(f'report: wrote {args.output}', file=sys.stderr)

    # Also write combined JSON version
    json_out = args.output.replace('.md', '.json')
    with open(json_out, 'w') as f:
        json.dump({
            'test_data': test_data,
            'llm_analysis': llm_response,
        }, f, indent=2)
    print(f'report: wrote {json_out}', file=sys.stderr)


if __name__ == '__main__':
    main()
