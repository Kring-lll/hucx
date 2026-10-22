#!/usr/bin/env bash
set -euo pipefail
archive=$(readlink -f "${1:?usage: build_isolated.sh SOURCE.tar.gz FULL_COMMIT}")
commit=${2:?full GitHub commit required}
[[ $commit =~ ^[0-9a-f]{40}$ ]]
lab=/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019
work=$(mktemp -d "$lab/r1_shared_${commit:0:9}_XXXXXX")
mkdir "$work/source" "$work/build" "$work/install"
trap 'rc=$?; if ((rc)); then printf "BUILD_FAILED=%s EXIT=%s\n" "$work" "$rc"; tail -n 16 "$work/build.log" 2>/dev/null || true; fi' EXIT
tar -xzf "$archive" -C "$work/source"
python3 - "$work/source" <<'PY'
import sys
from pathlib import Path
p=Path(sys.argv[1])/'autogen.sh'
assert b'\r' not in p.read_bytes().split(b'\n',1)[0], 'CRLF archive; use git archive with LF source'
import json,hashlib
root=Path(sys.argv[1])
manifest=json.loads((root/'contrib/rm_ctp/r1/candidate_manifest.json').read_text())
for name,expected in manifest['files'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected, 'reviewed source mismatch: '+name
PY
(
    cd "$work/source"
    ./autogen.sh
    cd "$work/build"
    "$work/source/configure" --prefix="$work/install" --with-ub=/usr \
        --disable-java --without-cuda --without-rocm --without-verbs --without-rdmacm \
        CC=gcc CXX=g++ CFLAGS='-O2 -g' CXXFLAGS='-O2 -g'
    make -j8
    make install
) > "$work/build.log" 2>&1
python3 - "$work" "$archive" "$commit" <<'PY'
import hashlib,json,sys
from pathlib import Path
w,a,c=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
m=w/'install/lib/ucx/libuct_ub.so.0.0.0'
assert m.is_file(), str(m)
d={'case_dir':str(w),'source_commit':c,'archive':str(a),
   'archive_sha256':hashlib.sha256(a.read_bytes()).hexdigest(),
   'module_sha256':hashlib.sha256(m.read_bytes()).hexdigest(),
   'validation':'BUILD_ONLY; hardware acceptance pending'}
(w/'build_manifest.json').write_text(json.dumps(d,indent=2)+'\n')
print('BUILD_PASS CASE_DIR='+str(w))
print('SOURCE_COMMIT='+c)
print('MODULE_SHA256='+d['module_sha256'])
PY
tar -czf "$work/install.tar.gz" -C "$work" install
printf '%s\n' "$work" > "$lab/r1_latest_candidate.txt"
printf 'BUILD_LOG=%s/build.log\n' "$work"
