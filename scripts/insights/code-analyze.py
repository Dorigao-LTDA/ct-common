#!/usr/bin/env python3
"""
Analyze code changes between HEAD and a base ref to provide context for LLM.

Extracts:
  - Changed files (by category: src, nfr, deploy, perf, pom)
  - Changed endpoints (Spring annotations in Java)
  - Dependency changes (pom.xml diffs)
  - Resource config changes (deploy/values.yaml)
  - NFR threshold changes (nfr.yaml)

Usage:
  python3 code-analyze.py --repo-path . --base-ref HEAD~1 --service catalogo \
                          --output insights-input-code.json

Graceful fallback: if base-ref is unavailable (e.g. shallow clone),
returns {"available": false, "reason": "..."}.
"""
import argparse
import json
import os
import re
import subprocess
import sys

# Regex for Spring request mapping annotations in Java source
_ENDPOINT_RE = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping)'
    r'\(?\s*'
    r'(?:'
    r'  (?:value|path)\s*=\s*\{?["\']([^"\'}]+)["\']\}?'
    r'|' 
    r'  ["\']([^"\']+)["\']'
    r')?'
    r'[^)]*\)?',
    re.VERBOSE,
)

# Files we care about categorizing
_CATEGORIES = {
    'src': 'src/main/java/',
    'src': 'src/main/resources/',
    'nfr': 'nfr.yaml',
    'deploy': 'deploy/',
    'perf': 'perf/',
    'chaos': 'chaos/',
    'pom': 'pom.xml',
}


def git(repo_path, *args):
    """Run git command and return (stdout, stderr)."""
    try:
        result = subprocess.run(
            ['git', '-C', repo_path] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return '', str(e)


def categorize_file(path):
    """Map file path to category label."""
    for cat, prefix in _CATEGORIES.items():
        if path.startswith(prefix) or path == prefix.rstrip('/'):
            return cat
    return 'other'


def extract_endpoints(diff_text):
    """
    Scan added lines in a Spring Java diff for endpoint annotations.
    Returns list of dicts with annotation, path, and approximate diff line.
    """
    endpoints = []
    for i, line in enumerate(diff_text.split('\n'), 1):
        if not line.startswith('+') or line.startswith('+++'):
            continue
        match = _ENDPOINT_RE.search(line[1:])  # strip + prefix
        if match:
            annotation = match.group(1)
            path = match.group(2) or match.group(3) or ''
            endpoints.append({
                'annotation': annotation,
                'path': path.strip('" '),
                'diff_line': i,
            })
    return endpoints


def extract_dependency_changes(diff_text):
    """Scan added lines in pom.xml diffs for dependency blocks."""
    # ponytail: simple regex extraction. Misses multi-line <dependency> blocks
    # across non-adjacent + lines, but covers 90%+ of real cases.
    deps = []
    for line in diff_text.split('\n'):
        if not line.startswith('+') or line.startswith('+++'):
            continue
        content = line[1:]
        aid = re.search(r'<artifactId>([^<]+)', content)
        ver = re.search(r'<version>([^<]+)', content)
        if aid:
            deps.append({
                'artifact': aid.group(1),
                'version': ver.group(1) if ver else 'unknown',
            })
    return deps


def extract_resource_changes(diff_text):
    """Scan deploy/*.yaml diffs for resource key changes."""
    changes = {}
    for pat in ['limits.cpu', 'limits.memory', 'requests.cpu',
                'requests.memory', 'replicas']:
        # Match lines like `-  memory: 512Mi` and `+  memory: 1Gi`
        minus = re.findall(rf'^-\s+{re.escape(pat)}:\s*(.+)', diff_text, re.MULTILINE)
        plus = re.findall(rf'^\+\s+{re.escape(pat)}:\s*(.+)', diff_text, re.MULTILINE)
        if minus and plus:
            changes[pat] = {'from': minus[-1].strip(), 'to': plus[-1].strip()}
    return changes


def main():
    parser = argparse.ArgumentParser(
        description='Analyze code changes for LLM context'
    )
    parser.add_argument('--repo-path', default='.',
                        help='Path to git repository')
    parser.add_argument('--base-ref', required=True,
                        help='Base ref for diff (e.g. HEAD~1 or merge-base)')
    parser.add_argument('--service', required=True,
                        help='Service name (catalogo, pagamento, pedido)')
    parser.add_argument('--output', default='insights-input-code.json')
    args = parser.parse_args()

    output = {'available': False, 'service': args.service}

    # Validate base ref exists
    stdout, stderr = git(args.repo_path, 'rev-parse', '--verify', '--quiet', args.base_ref)
    if stderr or not stdout:
        output['reason'] = f'Base ref "{args.base_ref}" not found (shallow clone?)'
        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)
        print(f'code-analyze: {output["reason"]}', file=sys.stderr)
        return

    # Diff stat
    stat, _ = git(args.repo_path, 'diff', '--stat', args.base_ref, 'HEAD')
    output['diff_summary'] = stat.split('\n')[-1] if stat else '0 files changed'

    # Changed files
    files_str, _ = git(args.repo_path, 'diff', '--name-only', args.base_ref, 'HEAD')
    changed_files = [f.strip() for f in files_str.split('\n') if f.strip()]
    output['changed_files'] = changed_files
    output['categories'] = {}
    for f in changed_files:
        cat = categorize_file(f)
        output['categories'].setdefault(cat, []).append(f)

    # Full diff (truncated)
    full_diff, _ = git(args.repo_path, 'diff', '--unified=3', args.base_ref, 'HEAD')
    output['diff_truncated'] = full_diff[:10000] if full_diff else ''

    # --- Full application source code (for LLM root-cause analysis) ---
    output['full_source'] = {}
    source_files, _ = git(args.repo_path, 'ls-files',
                          'src/main/java/', 'src/main/resources/',
                          'pom.xml', 'nfr.yaml', 'deploy/values.yaml')
    for f in [x.strip() for x in source_files.split('\n') if x.strip()]:
        if f.endswith(('.java', '.xml', '.yaml', '.yml', '.properties')):
            content, _ = git(args.repo_path, 'show', f'HEAD:{f}')
            if content:
                # ponytail: per-file cap to keep total input bounded.
                # Java service ~30 files × ~4KB avg = ~120KB → LLM context OK.
                output['full_source'][f] = content[:4000]

    # Endpoints from Java files
    output['changed_endpoints'] = []
    java_files = [f for f in changed_files
                  if f.startswith('src/main/java/') and f.endswith('.java')]
    for f in java_files:
        df, _ = git(args.repo_path, 'diff', args.base_ref, 'HEAD', '--', f)
        endpoints = extract_endpoints(df)
        if endpoints:
            output['changed_endpoints'].extend(endpoints)

    # Dependency changes from pom.xml
    output['dependency_changes'] = []
    for f in changed_files:
        if 'pom.xml' in f:
            df, _ = git(args.repo_path, 'diff', args.base_ref, 'HEAD', '--', f)
            output['dependency_changes'].extend(extract_dependency_changes(df))

    # Resource changes from deploy files
    output['resource_changes'] = {}
    for f in changed_files:
        if f.startswith('deploy/'):
            df, _ = git(args.repo_path, 'diff', args.base_ref, 'HEAD', '--', f)
            output['resource_changes'].update(extract_resource_changes(df))

    # NFR changes
    output['nfr_changes'] = {}
    for f in changed_files:
        if 'nfr.yaml' in f:
            df, _ = git(args.repo_path, 'diff', args.base_ref, 'HEAD', '--', f)
            # ponytail: mark nfr changes without full YAML diff parsing
            added = [l for l in df.split('\n') if l.startswith('+') and not l.startswith('+++')]
            removed = [l for l in df.split('\n') if l.startswith('-') and not l.startswith('---')]
            output['nfr_changes'] = {
                'added_lines': len(added),
                'removed_lines': len(removed),
                'samples': (removed[:3] + added[:3])[:5],
            }

    output['available'] = True

    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'code-analyze: wrote {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
