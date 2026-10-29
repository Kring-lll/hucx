"""Check the effective RM segment limit on both nodes, including UCX K units."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'a89'))
import run_scale as base

DEVICE = 'udmac0d1e2:1'
SEGMENT_KEY = 'UCX_RM_URMA_SEG_SIZE'


def bytes_value(value):
    match = re.fullmatch(r'\s*(\d+)([KMG]?)B?\s*', value, re.I)
    if not match:
        raise ValueError('unexpected UCX byte unit: ' + value)
    return int(match[1]) * 1024 ** ('', 'K', 'M', 'G').index(match[2].upper())


def parse_config(text, env):
    parsed = dict(re.findall(r'^(UCX_[A-Z0-9_]+)=(.*?)\s*$', text, re.M))
    for key, expected in env.items():
        if not key.startswith('UCX_RM_URMA_'):
            continue
        actual = parsed.get(key)
        if actual is None:
            raise ValueError('UCX setting not parsed: ' + key)
        if key == SEGMENT_KEY:
            equal = bytes_value(actual) == bytes_value(expected)
        elif key.endswith('_THRESH'):
            equal = float(actual) == float(expected)
        else:
            equal = actual.lower() == expected.lower()
        if not equal:
            raise ValueError('UCX setting differs: %s=%s expected=%s' %
                             (key, actual, expected))
    return parsed


def parse_capability(text, device=DEVICE):
    lines = text.splitlines()
    indexes = [i for i, line in enumerate(lines)
               if re.search(r'\bDevice:\s*' + re.escape(device) + r'\s*$', line)]
    if len(indexes) != 1:
        raise ValueError('target RM device missing or repeated: ' + device)
    start = indexes[0]
    end = next((i for i in range(start + 1, len(lines))
                if re.search(r'\b(?:Device|Transport):', lines[i])), len(lines))
    match = re.search(r'\bam_bcopy:\s*<=\s*(\d+)\b', '\n'.join(lines[start:end]))
    if not match:
        raise ValueError('target RM am_bcopy limit absent')
    return int(match[1])


def query_local(install, env):
    actual_env = {k: v for k, v in os.environ.items() if not k.startswith('UCX_')}
    actual_env.update(env)
    info = str(install / 'bin/ucx_info')
    outputs = []
    for flag in ('-c', '-d'):
        command = [info, flag] if flag == '-c' else [info, '-d', '-t', 'rm_urma']
        result = subprocess.run(command, env=actual_env, text=True,
                                capture_output=True, timeout=40)
        if result.returncode:
            raise ValueError('ucx_info %s EXIT=%d %s' %
                             (flag, result.returncode, result.stderr[-400:]))
        outputs.append(result.stdout)
    return {'config': outputs[0], 'device': outputs[1]}


def check_outputs(outputs, env, segment):
    config = parse_config(outputs['config'], env)
    limit = parse_capability(outputs['device'])
    if bytes_value(config[SEGMENT_KEY]) != segment or limit != segment - 3:
        raise ValueError('RM AM limit inconsistent with selected segment size')
    return {'segment_bytes': segment, 'am_bcopy_bytes': limit,
            'effective_segment': config[SEGMENT_KEY]}


def parsed_config_with_units(install, env, directory, mode):
    if mode != 'iface' or bytes_value(env[SEGMENT_KEY]) != 4096:
        raise ValueError('unsupported boundary trial configuration')
    local = query_local(install, env)
    script = ('import json,os,subprocess,sys\n'
              'e={k:v for k,v in os.environ.items() if not k.startswith("UCX_")}\n'
              'e.update(json.loads(sys.argv[2]))\n'
              'info=sys.argv[1]+"/bin/ucx_info"\n'
              'result={}\n'
              'for name,argv in (("config",[info,"-c"]),("device",[info,"-d","-t","rm_urma"])):\n'
              ' p=subprocess.run(argv,env=e,text=True,capture_output=True,timeout=40)\n'
              ' if p.returncode: raise RuntimeError(name+":"+p.stderr[-400:])\n'
              ' result[name]=p.stdout\n'
              'print(json.dumps(result))\n')
    peer = json.loads(base.ssh('exec python3 -c "$1" "$2" "$3"\n', script,
                               install, json.dumps(env), timeout=110))
    for host, output in (('node89', local), ('node97', peer)):
        (directory / (host + '_ucx_config.txt')).write_text(output['config'])
        (directory / (host + '_ucx_device.txt')).write_text(output['device'])
        check_outputs(output, env, 4096)
    return True
