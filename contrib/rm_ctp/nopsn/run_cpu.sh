#!/bin/bash
set -euo pipefail
src=$(cd "$(dirname "$0")/../../.." && pwd)
build=$(readlink -f "${1:?build dir}")
sdk=$(readlink -f "${2:?SDK prefix}")
libpath="$build/src/uct/.libs:$build/src/ucs/.libs:$build/src/ucm/.libs:$sdk/lib"
${CC:-gcc} -std=gnu99 -O1 -g -DHAVE_CONFIG_H -fsanitize=address,undefined \
 -fno-omit-frame-pointer -ffunction-sections -fdata-sections \
 -I"$build" -I"$build/src" -I"$src/src" -I"$sdk/include" \
 "$src/contrib/rm_ctp/nopsn/state_test.c" "$build/src/uct/urma/.libs/libuct_ub.a" \
 -L"$build/src/uct/.libs" -L"$build/src/ucs/.libs" -L"$build/src/ucm/.libs" \
 -L"$sdk/lib" -Wl,--gc-sections -Wl,-rpath,"$libpath" \
 -luct -lucs -lucm -lurma -lurma_common -lpthread -ldl -lrt -lm \
 -o "$build/nopsn_state_test"
UBSAN_OPTIONS=halt_on_error=1 ASAN_OPTIONS=detect_leaks=0 UCX_LOG_LEVEL=error "$build/nopsn_state_test"
UBSAN_OPTIONS=halt_on_error=1 ASAN_OPTIONS=detect_leaks=0 UCX_LOG_LEVEL=error python3 - "$build/nopsn_state_test" <<'PY'
import subprocess,sys,signal
p=subprocess.run([sys.argv[1],'--retry'],text=True,capture_output=True)
assert p.returncode==-signal.SIGABRT,(p.returncode,p.stdout,p.stderr)
assert 'normal-path experiment attempted DATA retransmission; sample invalid' in p.stdout+p.stderr
print('ACTUAL_DATA_RETRY_REFUSED=PASS')
PY
