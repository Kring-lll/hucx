"""Strict UCX v2 accum reader, Linux little endian with 64-bit size_t only.

Times are instrumented elapsed intervals, inclusive of nested scopes and OS
descheduling. They are NOT CPU samples, exclusive time or uninstrumented cost.
"""
import struct
from pathlib import Path

HEADER = struct.Struct('<I1024s1024s64sIIIIQ')
LOCATION = struct.Struct('<64s64s32siB')
THREAD = struct.Struct('<IQQQ')
ACCUM = struct.Struct('<QQ')


def string(value):
    return value.split(b'\0', 1)[0].decode('utf-8', errors='strict')


def read(path, pid, hostname, install):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 16*1024*1024:
        raise ValueError('missing/nonregular/oversized UCX accum file')
    data = path.read_bytes()
    offset = 0
    def take(fmt):
        nonlocal offset
        if offset + fmt.size > len(data):
            raise ValueError('truncated UCX accum file')
        value = fmt.unpack_from(data, offset)
        offset += fmt.size
        return value
    version, library, command, host, actual_pid, mode, locations, threads, second = take(HEADER)
    if (version != 2 or mode != 1 or actual_pid != pid or string(host) != hostname or
            not 0 < locations <= 2048 or not 0 < threads <= 128 or not 0 < second <= 10**12):
        raise ValueError('UCX version/mode/process/clock/count identity differs')
    if Path(string(library)).resolve().parent != (Path(install)/'lib').resolve():
        raise ValueError('UCX accum library not from diagnostic installation')
    descriptors = [take(LOCATION) for _ in range(locations)]
    totals = {}
    tids = []
    for _ in range(threads):
        tid, begin, end, records = take(THREAD)
        if tid in tids or end < begin or records:
            raise ValueError('duplicate thread, invalid interval or LOG mode records')
        tids.append(tid)
        for file, function, name, line, kind in descriptors:
            ticks, count = take(ACCUM)
            label = string(name)
            if label.startswith('rmc_') and kind == 2:
                if not count and ticks:
                    raise ValueError('elapsed time without calls')
                key = (string(file), string(function), label, line)
                row = totals.setdefault(key, dict(name=label, file=key[0], function=key[1],
                                                  line=line, calls=0, total_ns=0.0))
                row['calls'] += count
                row['total_ns'] += ticks * 1e9 / second
    if offset != len(data) or not totals or not any(v['calls'] for v in totals.values()):
        raise ValueError('extra data or no exercised diagnostic scopes')
    scopes = []
    for row in totals.values():
        row['mean_ns'] = row['total_ns']/row['calls'] if row['calls'] else 0
        scopes.append(row)
    return dict(pid=pid, hostname=hostname, ucs_library=string(library),
                command=string(command), one_second_ticks=second, tids=tids,
                scope_semantics='inclusive elapsed, overlaps; includes instrumentation and descheduling',
                scopes=sorted(scopes, key=lambda r:r['total_ns'], reverse=True))
