#!/usr/bin/env bash
# Build and test one uploaded, explicitly identified candidate. No latest pointer.
set -euo pipefail
archive=$(readlink -f "${1:?SOURCE.tar.gz FULL_COMMIT ARCHIVE_SHA256}")
commit=${2:?full commit required}
archive_sha=${3:?archive SHA256 required}
[[ $commit =~ ^[0-9a-f]{40}$ ]]
[[ $archive_sha =~ ^[0-9a-f]{64}$ ]]
[[ $(id -u) == 0 && $(hostname) == node89 ]]
lab=/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev
[[ $archive == "$lab/upload/hucx-b-default-${commit:0:9}-source.tar.gz" ]]
test -f "$archive"
printf '%s  %s\n' "$archive_sha" "$archive" | sha256sum -c -
export HUCX_RM_CTP_EXPERIMENT_ROOT="$lab"
build_receipt=$(mktemp "$lab/default_segment_build_XXXXXX.log")
tar -xOf "$archive" contrib/rm_ctp/r1/build_isolated.sh |
    bash -s -- "$archive" "$commit" | tee "$build_receipt"
case_dir=$(python3 - "$build_receipt" "$lab" "$commit" <<'PY'
from pathlib import Path
import json
import sys

log, root, commit = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
rows = [line[len('BUILD_PASS CASE_DIR='):]
        for line in log.read_text().splitlines() if line.startswith('BUILD_PASS CASE_DIR=')]
assert len(rows) == 1, 'this build did not return exactly one CASE'
case = Path(rows[0]).resolve()
assert case.parent == root and case.name.startswith('r1_shared_' + commit[:9] + '_')
receipt = json.loads((case / 'build_manifest.json').read_text())
assert receipt['source_commit'] == commit and receipt['case_dir'] == str(case)
print(case)
PY
)
printf 'BUILD_RECEIPT=%s\n' "$build_receipt"
python3 -u "$case_dir/source/contrib/rm_ctp/segment_default/run_candidate.py" "$case_dir" --run
