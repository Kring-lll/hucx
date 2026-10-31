#!/usr/bin/env bash
set -euo pipefail
src=$(cd "$(dirname "$0")/../../.." && pwd)
build=$(cd "${1:?usage: run_cpu.sh BUILD_DIR URMA_INCLUDE}" && pwd)
sdk=${2:?URMA include directory required}
receipt=$(mktemp "$build/read_cpu_receipt_XXXXXX.log")
bash "$src/contrib/rm_ctp/r1/run_mock.sh" "$build" "$sdk" | tee "$receipt"
out=$(sed -n 's/^MOCK_LOG_DIR=//p' "$receipt")
test -d "$out"
${CC:-gcc} -std=gnu99 -O1 -g -DHAVE_CONFIG_H -fsanitize=address,undefined \
    -fno-omit-frame-pointer -I"$build" -I"$build/src" -I"$src/src" -I"$sdk" \
    "$src/contrib/rm_ctp/native_read/test_native_read.c" "$out/weak.c" \
    "$build/src/uct/urma/.libs/libuct_ub.a" \
    -L"$build/src/uct/.libs" -L"$build/src/ucs/.libs" -L"$build/src/ucm/.libs" \
    -luct -lucs -lucm -lpthread -ldl -lrt -lm \
    -Wl,-rpath,"$build/src/uct/.libs:$build/src/ucs/.libs:$build/src/ucm/.libs" \
    -o "$out/test_native_read"
UCX_LOG_LEVEL=error ASAN_OPTIONS=${ASAN_OPTIONS:-detect_leaks=0} "$out/test_native_read"
