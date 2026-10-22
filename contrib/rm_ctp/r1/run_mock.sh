#!/usr/bin/env bash
set -euo pipefail
src=$(cd "$(dirname "$0")/../../.." && pwd)
build=$(cd "${1:?usage: run_mock.sh BUILD_DIR [URMA_INCLUDE]}" && pwd)
sdk=${2:-/usr/include/ub/umdk/urma}
out=$(mktemp -d "$build/r1_mock.XXXXXX")
archive="$build/src/uct/urma/.libs/libuct_ub.a"
test -f "$archive"
python3 - "$archive" "$out/weak.c" <<'PY'
import re, subprocess, sys
from pathlib import Path
symbols = set(re.findall(r'\bU\s+(urma_\w+)',
                        subprocess.check_output(['nm', '-u', sys.argv[1]], text=True)))
mocked = {'urma_post_jetty_send_wr', 'urma_modify_jetty',
          'urma_unimport_jetty', 'urma_unimport_seg', 'urma_init', 'urma_uninit'}
lines = ['#include <stdio.h>', '#include <stdlib.h>',
         'void r1_unexpected_urma_call(void) { fputs("Unexpected URMA API in CPU mock\\n", stderr); abort(); }']
for name in sorted(symbols - mocked):
    lines.append('__attribute__((weak)) void '+name+'(void) { fputs("Unexpected '+name+' in CPU mock\\n", stderr); abort(); }')
Path(sys.argv[2]).write_text('\n'.join(lines)+'\n')
PY
${CC:-gcc} -std=gnu99 -O1 -g -DHAVE_CONFIG_H -fsanitize=address,undefined \
    -fno-omit-frame-pointer -I"$build" -I"$build/src" -I"$src/src" -I"$sdk" \
    "$src/contrib/rm_ctp/r1/test_shared_tx.c" "$out/weak.c" \
    "$archive" -L"$build/src/uct/.libs" -L"$build/src/ucs/.libs" \
    -L"$build/src/ucm/.libs" -luct -lucs -lucm -lpthread -ldl -lrt -lm \
    -Wl,-rpath,"$build/src/uct/.libs:$build/src/ucs/.libs:$build/src/ucm/.libs" \
    -o "$out/test_shared_tx"
# Some Work runtimes restrict /proc task enumeration used by LeakSanitizer.
# ASan/UBSan remain enabled; opt in to LSan with ASAN_OPTIONS=detect_leaks=1.
UCX_LOG_LEVEL=error ASAN_OPTIONS=${ASAN_OPTIONS:-detect_leaks=0} "$out/test_shared_tx"
printf 'MOCK_LOG_DIR=%s\n' "$out"
