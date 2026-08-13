#!/usr/bin/env python3
"""
Call an LLM provider using the OpenAI Python library via Azure AI Foundry.

3 sequential calls:
  1. Performance Analyst — k6 metrics + thresholds + observability correlation
  2. Resilience Analyst — chaos recovery + error logs
  3. Synthesis — combines both + code changes into developer report

Usage:
  export LLM_ENDPOINT="https://dorigao-ltda-openai.services.ai.azure.com/openai/v1"
  export LLM_DEPLOYMENT="DeepSeek-V4-Flash"
  export LLM_API_KEY="..."    # or set via OIDC token in the pipeline

  python3 analyze.py --input insights-input.json --output insights-llm-response.json

Graceful: missing env vars or API errors produce a partial JSON with error,
never exit with non-zero.
"""
import argparse
import json
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


SYSTEM_PROMPT = """You are a Senior Site Reliability Engineer (SRE) specialized in distributed systems,
performance optimization, and chaos engineering on Kubernetes.

You analyze test results from a Continuous Testing Platform running on AKS with ArgoCD,
k6 for performance testing, Chaos Mesh for resilience testing, and a Grafana LGTM stack
(Mimir/Loki/Tempo/Pyroscope) for observability.

The input contains:
- test_results: k6 performance metrics (smoke, baseline, stress, spike) + chaos recovery data
- thresholds: nfr.yaml performance and resilience thresholds
- gates: pass/fail status per gate with actual vs threshold values
- observability: (optional) Mimir metrics (CPU/Memory/GC/JVM), Loki logs (errors/warnings/OOM),
  Tempo traces (slow/error traces) collected during the test window
- code_changes: (optional) git diff summary, changed endpoints, dependency changes, resource config changes

Your tasks, in priority order:
1. Identify root causes for any FAILED gates using ALL available evidence (k6 + observability + code)
2. If observability data is available, correlate k6 metric degradations with:
   - CPU throttling or memory pressure (container_cpu_usage_pct, container_memory_working_set_pct)
   - GC pressure (jvm_gc_rate spikes, jvm_heap_usage_pct > 80%)
   - Error logs or OOM signals (Loki ERROR/OOMKilled lines)
   - Slow traces (Tempo traces > 1s)
3. If code changes are available, identify which modified endpoints or dependencies likely
   caused the performance change
4. For each finding, provide a concrete, actionable recommendation with:
   - Priority: critical (gate failure), warning (within 20% of threshold), or info (optimization)
   - Exact file path and config key to change (from nfr.yaml, deploy/values.yaml, or source code)
   - Suggested value with justification
   - If the change is in JVM args, Dockerfile, or pom.xml -- cite the exact parameter

Rules:
- ONLY suggest changes to files and configs that are present in the provided data
- NEVER fabricate metrics, endpoints, or file paths
- If you cannot determine the root cause with confidence, state your uncertainty explicitly
- Prefer resource tuning (limits, requests, JVM args) over architectural changes
- If memory pressure > 85% during test, recommend increasing limits.memory BEFORE any code change

Output format: valid JSON with the fields: {"findings": [...], "executive_summary": "..."}
Each finding has: title, severity (critical/warning/info), metric, threshold, actual,
hypothesis, recommendation

Return ONLY the JSON object, no markdown fences, no extra text."""


def call_llm(messages, base_url, api_key, deployment, timeout=60):
    """
    Make one LLM chat completion call via OpenAI-compatible API (Azure AI Foundry).
    Returns parsed JSON response dict or dict with 'error' key.
    """
    if OpenAI is None:
        return {'error': 'openai library not installed. Run: pip install openai'}

    if not api_key:
        return {'error': 'no API key or token provided'}

    try:
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
        )
    except Exception as e:
        return {'error': f'failed to create OpenAI client: {e}'}

    try:
        completion = client.chat.completions.create(
            model=deployment,
            messages=messages,
            temperature=0.1,
            max_tokens=2000,
            response_format={'type': 'json_object'},
            timeout=timeout,
        )
    except Exception as e:
        return {'error': f'API call failed: {e}'}

    choices = completion.choices
    if not choices:
        return {'error': 'empty response choices'}

    content = choices[0].message.content
    if not content:
        return {'error': 'empty message content'}

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        return {'error': f'LLM output not valid JSON: {e}', 'raw': content[:1000]}


def build_prompts(data, role, instructions):
    """Build system + user message list for a given analysis role."""
    sys_msg = SYSTEM_PROMPT
    user_msg = f'Role: {role}\n\nInstructions: {instructions}\n\n'
    # ponytail: full_source (app code) can be large; DeepSeek handles big context.
    # 40000 chars covers test metrics + observability + full Java service source.
    user_msg += json.dumps(data, indent=2)[:40000]
    return [
        {'role': 'system', 'content': sys_msg},
        {'role': 'user', 'content': user_msg},
    ]


def main():
    parser = argparse.ArgumentParser(
        description='Run LLM analysis on test data (OpenAI library)'
    )
    parser.add_argument('--input', required=True,
                        help='insights-input.json from collect.py')
    parser.add_argument('--output', default='insights-llm-response.json',
                        help='Output file for LLM JSON response')
    parser.add_argument('--timeout', type=int, default=60,
                        help='Timeout per LLM call in seconds')
    args = parser.parse_args()

    # Load input
    if not os.path.exists(args.input):
        fallback = {'error': f'Input file not found: {args.input}', 'findings': []}
        with open(args.output, 'w') as f:
            json.dump(fallback, f, indent=2)
        print(f'analyze: ERROR {fallback["error"]}', file=sys.stderr)
        return

    try:
        with open(args.input) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        fallback = {'error': f'Invalid or unreadable input: {e}', 'findings': []}
        with open(args.output, 'w') as f:
            json.dump(fallback, f, indent=2)
        print(f'analyze: {fallback["error"]}', file=sys.stderr)
        return

    # Read env
    base_url = os.environ.get('LLM_ENDPOINT', '')
    api_key = os.environ.get('LLM_API_KEY', '')
    deployment = os.environ.get('LLM_DEPLOYMENT', 'DeepSeek-V4-Flash')

    if not base_url:
        fallback = {
            'error': 'LLM_ENDPOINT not set. Set LLM_ENDPOINT, LLM_API_KEY, and LLM_DEPLOYMENT',
            'findings': [],
            'executive_summary': 'LLM analysis unavailable — LLM endpoint not configured.',
        }
        with open(args.output, 'w') as f:
            json.dump(fallback, f, indent=2)
        print('analyze: LLM_ENDPOINT not set, wrote fallback', file=sys.stderr)
        return

    if not api_key:
        fallback = {
            'error': 'LLM_API_KEY not set. Provide via OIDC token or secrets.LLM_API_KEY',
            'findings': [],
            'executive_summary': 'LLM analysis unavailable — no authentication token.',
        }
        with open(args.output, 'w') as f:
            json.dump(fallback, f, indent=2)
        print('analyze: LLM_API_KEY not set, wrote fallback', file=sys.stderr)
        return

    # ---- Call 1: Performance Analyst ----
    perf_prompt = (
        'Analyze the performance test results (k6 metrics) and observability data (Mimir, Loki). '
        'Identify: (a) which gates failed or are close to failing, '
        '(b) root causes from observability correlation, '
        '(c) concrete recommendations with file paths and values.'
    )
    perf_messages = build_prompts(data, 'Performance Analyst', perf_prompt)
    perf_result = call_llm(perf_messages, base_url, api_key, deployment, args.timeout)

    # ---- Call 2: Resilience Analyst ----
    resil_prompt = (
        'Analyze the resilience test results (chaos recovery times) and related logs. '
        'Identify: (a) experiments where recovery exceeded thresholds, '
        '(b) patterns in error logs during experiments, '
        '(c) recommendations to improve recovery time or resilience configuration.'
    )
    resil_messages = build_prompts(data, 'Resilience Analyst', resil_prompt)
    resil_result = call_llm(resil_messages, base_url, api_key, deployment, args.timeout)

    # ---- Call 3: Synthesis (Tech Lead) ----
    synthesis_context = {
        'performance_analysis': perf_result,
        'resilience_analysis': resil_result,
        'code_changes': data.get('code_changes', {}),
        'gates': data.get('gates', []),
    }
    synth_prompt = (
        'Synthesize the two analyses above (performance + resilience) along with the code changes '
        'and gate results into a unified developer report. '
        'Prioritize findings by severity. '
        'Provide an executive summary (3-5 bullets) with the most important action items. '
        'Output format: {"findings": [...], "executive_summary": "..."}'
    )
    synth_messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': synth_prompt},
        {'role': 'user', 'content': json.dumps(synthesis_context, indent=2)[:30000]},
    ]
    synth_result = call_llm(synth_messages, base_url, api_key, deployment, args.timeout)

    # ---- Assemble final output ----
    result = {
        'performance_analysis': perf_result if 'error' not in perf_result else perf_result,
        'resilience_analysis': resil_result if 'error' not in resil_result else resil_result,
        'findings': synth_result.get('findings', []) if 'error' not in synth_result else [],
        'executive_summary': synth_result.get('executive_summary', '') if 'error' not in synth_result else '',
        'errors': {},
    }

    for name, res in [('performance', perf_result), ('resilience', resil_result),
                       ('synthesis', synth_result)]:
        if 'error' in res:
            result['errors'][name] = res['error']

    if not result['executive_summary'] and not result['findings']:
        result['executive_summary'] = 'LLM analysis unavailable — all API calls failed or returned empty.'

    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2)
    errs = list(result['errors'].keys())
    print(f'analyze: wrote {args.output} (errors: {errs if errs else "none"})', file=sys.stderr)


if __name__ == '__main__':
    main()
