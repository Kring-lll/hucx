#!/usr/bin/env bash
set -euo pipefail
src=$(cd "$(dirname "$0")/../../.." && pwd)
build=$(cd "${1:?usage: run_cpu.sh BUILD_DIR [URMA_INCLUDE]}" && pwd)
sdk=${2:-/usr/include/ub/umdk/urma}
bash "$src/contrib/rm_ctp/r1/run_mock.sh" "$build" "$sdk"
out=$(mktemp -d "$build/send_reliable_cpu.XXXXXX")
archive="$build/src/uct/urma/.libs/libuct_ub.a"
python3 - "$archive" "$out/weak.c" <<'PY'
import re
import subprocess
import sys
from pathlib import Path
symbols = set(re.findall(r'\bU\s+(urma_\w+)', subprocess.check_output(['nm', '-u', sys.argv[1]], text=True)))
mocked = {'urma_post_jetty_send_wr', 'urma_post_jfr_wr', 'urma_poll_jfc', 'urma_modify_jetty',
          'urma_unimport_jetty', 'urma_unimport_seg', 'urma_init', 'urma_uninit',
          'urma_import_jetty', 'urma_import_seg', 'urma_delete_jetty', 'urma_delete_jfr'}
lines = ['#include <stdio.h>', '#include <stdlib.h>']
for name in sorted(symbols - mocked):
    lines.append('__attribute__((weak)) void ' + name + '(void) { fputs("Unexpected URMA call\\n", stderr); abort(); }')
Path(sys.argv[2]).write_text('\n'.join(lines) + '\n')
PY
for checks in on off; do
    headers="$build"
    if [ "$checks" = off ]; then
        headers="$out/assert_off"
        mkdir "$headers"
        python3 - "$build/config.h" "$headers/config.h" <<'PYCODE'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text()
for name in ('ENABLE_ASSERT', 'ENABLE_PARAMS_CHECK'):
    text = text.replace('#define ' + name + ' 1', '#define ' + name + ' 0')
Path(sys.argv[2]).write_text(text)
PYCODE
    fi
    ${CC:-gcc} -std=gnu99 -O1 -g -DHAVE_CONFIG_H -fsanitize=address,undefined \
        -fno-omit-frame-pointer -I"$headers" -I"$build" -I"$build/src" -I"$src/src" -I"$sdk" \
        "$src/contrib/rm_ctp/send_reliable/test_reliable.c" "$out/weak.c" \
        "$archive" -L"$build/src/uct/.libs" -L"$build/src/ucs/.libs" \
        -L"$build/src/ucm/.libs" -luct -lucs -lucm -lpthread -ldl -lrt -lm \
        -Wl,-rpath,"$build/src/uct/.libs:$build/src/ucs/.libs:$build/src/ucm/.libs" \
        -o "$out/test_reliable_$checks"
    UCX_LOG_LEVEL=error ASAN_OPTIONS=${ASAN_OPTIONS:-detect_leaks=0} "$out/test_reliable_$checks"
    printf 'ASSERT_PARAMS=%s ASAN_UBSAN=PASS\n' "$checks"
done
printf 'SEND_CPU_LOG_DIR=%s\n' "$out"
# Confirm the observer resolves a locally loaded real transport module. The
# compile-only SDK may lack API exports; provide aborting stubs, never a device.
python3 - "$build/src/uct/urma/.libs/libuct_ub.so" "$out/dso_weak.c" <<'PYCODE'
import re
import subprocess
import sys
from pathlib import Path
symbols = set(re.findall(r'\bU\s+(urma_\w+)', subprocess.check_output(['nm', '-D', sys.argv[1]], text=True)))
lines = ['#include <stdlib.h>', 'int urma_init(void *p) { return 0; }', 'int urma_uninit(void) { return 0; }']
for name in sorted(symbols - {'urma_init', 'urma_uninit'}):
    lines.append('void ' + name + '(void) { abort(); }')
Path(sys.argv[2]).write_text('\n'.join(lines) + '\n')
PYCODE
${CC:-gcc} -shared -fPIC "$out/dso_weak.c" -o "$out/dso_weak.so"
${CC:-gcc} -std=gnu99 -Wall -Wextra -Werror -fPIC -shared \
    "$src/contrib/rm_ctp/send_reliable/drop_receive.c" -ldl -o "$out/drop.so"
${CC:-gcc} -std=gnu99 -DHAVE_CONFIG_H -I"$build" -I"$build/src" -I"$src/src" -I"$sdk" \
    "$src/contrib/rm_ctp/send_reliable/test_interpose.c" -ldl -o "$out/test_interpose"
LD_LIBRARY_PATH="$build/src/uct/.libs:$build/src/ucs/.libs:$build/src/ucm/.libs:$sdk/../../../../lib:$sdk/../../../../lib64:${LD_LIBRARY_PATH:-}" \
LD_PRELOAD="$out/drop.so:$out/dso_weak.so" \
HUCX_SEND_TEST_MODULE="$build/src/uct/urma/.libs/libuct_ub.so" \
    "$out/test_interpose" "$build/src/uct/urma/.libs/libuct_ub.so"
