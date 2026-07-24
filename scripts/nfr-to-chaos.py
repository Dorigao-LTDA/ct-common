#!/usr/bin/env python3
"""
Generate Chaos Mesh manifests from nfr.yaml resilience.chaos_experiments.
Makes nfr.yaml the single source of truth for chaos configuration.
No static chaos/*.yaml needed — pipeline generates manifests at runtime.

Usage: python3 scripts/nfr-to-chaos.py --nfr nfr.yaml --service <name> --output-dir chaos-generated

Output per experiment: <output-dir>/<short-name>.yaml (Chaos Mesh manifest)
                     + <output-dir>/manifest-list.json (list for pipeline iteration)

Supported chaos types: PodChaos, NetworkChaos, StressChaos
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


def gen_manifest(exp, service):
    """Build a Chaos Mesh manifest dict from an nfr.yaml experiment entry."""
    kind = exp.get('type', 'PodChaos')
    name = exp.get('name', f'{service}-unknown')
    duration = exp.get('duration', '60s')

    # Common fields
    manifest = {
        'apiVersion': 'chaos-mesh.org/v1alpha1',
        'kind': kind,
        'metadata': {
            'name': name,
            'namespace': 'app',
            'labels': {
                'app.kubernetes.io/name': service,
                'chaos-test': 'true',
            },
        },
        'spec': {
            'mode': 'one' if kind != 'NetworkChaos' else 'all',
            'selector': {
                'namespaces': ['app'],
                'labelSelectors': {'app': service},
            },
            'duration': duration,
        },
    }

    # ponytail: scheduler omitted — pipeline controls timing via apply + sleep + delete.
    # Scheduler ("@every 5m" etc.) is only useful for live clusters, not CI.

    if kind == 'PodChaos':
        manifest['spec']['action'] = 'pod-kill'

    elif kind == 'NetworkChaos':
        manifest['spec']['action'] = 'delay'
        manifest['spec']['delay'] = {
            'latency': exp.get('delay', '100ms'),
            'correlation': exp.get('correlation', '0.5'),
            'jitter': exp.get('jitter', '20ms'),
        }

    elif kind == 'StressChaos':
        stressors = {}
        cpu = exp.get('cpu', {})
        if cpu:
            stressors['cpu'] = {
                'workers': cpu.get('workers', 1),
                'load': cpu.get('load', 80),
            }
        else:
            # Default CPU stress when nfr.yaml doesn't specify cpu params
            stressors['cpu'] = {'workers': 1, 'load': 80}
        memory = exp.get('memory', {})
        if memory:
            stressors['memory'] = {
                'workers': memory.get('workers', 1),
                'size': memory.get('size', '256MB'),
            }
        manifest['spec']['stressors'] = stressors

    else:
        raise ValueError(
            f'Unsupported chaos type "{kind}" for experiment "{name}". '
            f'Supported: PodChaos, NetworkChaos, StressChaos'
        )

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description='Generate Chaos Mesh manifests from nfr.yaml'
    )
    parser.add_argument('--nfr', required=True, help='Path to nfr.yaml')
    parser.add_argument('--service', required=True, help='Service name (catalogo, pagamento, pedido)')
    parser.add_argument('--output-dir', default='chaos-generated',
                        help='Output directory (default: chaos-generated)')
    args = parser.parse_args()

    nfr = parse_yaml(args.nfr)
    experiments = nfr.get('resilience', {}).get('chaos_experiments', [])

    if not experiments:
        print('nfr-to-chaos: no resilience.chaos_experiments found in nfr.yaml',
              file=sys.stderr)
        # Create empty manifest list so pipeline can handle gracefully
        os.makedirs(args.output_dir, exist_ok=True)
        with open(os.path.join(args.output_dir, 'manifest-list.json'), 'w') as f:
            json.dump({'experiments': []}, f)
        return

    import yaml

    os.makedirs(args.output_dir, exist_ok=True)
    manifest_list = []

    for exp in experiments:
        exp_name = exp.get('name', '')
        # Strip service prefix: "catalogo-pod-kill" -> short name "pod-kill"
        short_name = exp_name
        if short_name.startswith(f'{args.service}-'):
            short_name = short_name[len(args.service) + 1:]

        if not short_name:
            print(f'nfr-to-chaos: SKIPPING experiment with empty name',
                  file=sys.stderr)
            continue

        filename = f'{short_name}.yaml'
        filepath = os.path.join(args.output_dir, filename)

        try:
            manifest = gen_manifest(exp, args.service)
        except ValueError as e:
            print(f'nfr-to-chaos: SKIPPING {exp_name} — {e}', file=sys.stderr)
            continue

        # dump as block-style YAML (default_flow_style=False) preserving field order
        output = yaml.dump(manifest, default_flow_style=False, sort_keys=False)

        with open(filepath, 'w') as f:
            f.write(output)

        manifest_list.append({
            'name': short_name,
            'file': filepath,
            'type': manifest['kind'],
        })

        print(f'nfr-to-chaos: generated {filepath}', file=sys.stderr)

    # Write manifest list for pipeline iteration
    list_path = os.path.join(args.output_dir, 'manifest-list.json')
    with open(list_path, 'w') as f:
        json.dump({'experiments': manifest_list}, f, indent=2)

    print(f'nfr-to-chaos: generated {len(manifest_list)} manifests in {args.output_dir}',
          file=sys.stderr)


if __name__ == '__main__':
    main()
