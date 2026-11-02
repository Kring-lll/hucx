/* MPI window correctness gate. Payload uses MPI_Put/Get; synchronization is
 * explicit. Full exposed/local allocations are checked after every operation. */
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { CAPACITY = 65536, GUARD = 128, TOTAL = CAPACITY + 2 * GUARD };
static int rank_id;

static void checked(int status, const char *operation)
{
    if (status != MPI_SUCCESS) {
        char message[MPI_MAX_ERROR_STRING];
        int length = 0;
        MPI_Error_string(status, message, &length);
        fprintf(stderr, "MPI_RMA_ERROR rank=%d operation=%s error=%.*s\n",
                rank_id, operation, length, message);
        MPI_Abort(MPI_COMM_WORLD, 2);
        exit(2);
    }
}

static unsigned char pattern(unsigned generation, unsigned round, size_t index)
{
    return (unsigned char)(generation * 41u + round * 37u + index * 13u + (index >> 8));
}

static int verify(const unsigned char *data, size_t offset, size_t size,
                  int contains_payload, unsigned generation, unsigned round,
                  const char *label)
{
    for (size_t i = 0; i < TOTAL; ++i) {
        unsigned char expected = 0xa5;
        if (contains_payload && i >= GUARD + offset && i < GUARD + offset + size) {
            expected = pattern(generation, round, i - GUARD - offset);
        }
        if (data[i] != expected) {
            fprintf(stderr, "MPI_RMA_MISMATCH rank=%d generation=%u round=%u "
                    "buffer=%s index=%zu expected=%u actual=%u\n",
                    rank_id, generation, round, label, i,
                    (unsigned)expected, (unsigned)data[i]);
            return 0;
        }
    }
    return 1;
}

static void consensus(int good)
{
    int all_good;
    checked(MPI_Allreduce(&good, &all_good, 1, MPI_INT, MPI_MIN, MPI_COMM_WORLD),
            "validation consensus");
    if (!all_good) {
        MPI_Abort(MPI_COMM_WORLD, 3);
        exit(3);
    }
}

static void data_rounds(int passive)
{
    const size_t sizes[] = {8, 65536, 64, 16385, 4096, 8193, 8, 65536};
    const char *sync = passive ? "lock_all" : "fence";
    unsigned char *exposed = malloc(TOTAL);
    unsigned char *local = malloc(TOTAL);
    MPI_Win win;
    consensus(exposed != NULL && local != NULL);
    for (unsigned generation = 0; generation < 2; ++generation) {
        int origin = (int)generation;
        int target = 1 - origin;
        memset(exposed, 0xa5, TOTAL);
        checked(MPI_Win_create(exposed, TOTAL, 1, MPI_INFO_NULL, MPI_COMM_WORLD, &win),
                "Win_create");
        checked(MPI_Win_set_errhandler(win, MPI_ERRORS_RETURN), "Win_set_errhandler");
        if (passive) {
            checked(MPI_Win_lock_all(0, win), "Win_lock_all");
        }
        for (unsigned round = 0; round < 16; ++round) {
            /* PUT and GET each traverse the same alternating size sequence. */
            int get = round >= 8;
            size_t size = sizes[round % 8];
            size_t offset = size == CAPACITY ? 0 : (round % 3) * 8;
            memset(exposed, 0xa5, TOTAL);
            memset(local, 0xa5, TOTAL);
            if ((get && rank_id == target) || (!get && rank_id == origin)) {
                unsigned char *destination = get ? exposed : local;
                for (size_t i = 0; i < size; ++i) {
                    destination[GUARD + offset + i] = pattern(generation, round, i);
                }
            }
            if (passive) {
                checked(MPI_Win_sync(win), "publish local stores");
                checked(MPI_Barrier(MPI_COMM_WORLD), "before operation");
            } else {
                checked(MPI_Win_fence(0, win), "opening fence");
            }
            if (rank_id == origin) {
                if (get) {
                    checked(MPI_Get(local + GUARD + offset, (int)size, MPI_BYTE,
                                    target, GUARD + (MPI_Aint)offset,
                                    (int)size, MPI_BYTE, win), "Get");
                } else {
                    checked(MPI_Put(local + GUARD + offset, (int)size, MPI_BYTE,
                                    target, GUARD + (MPI_Aint)offset,
                                    (int)size, MPI_BYTE, win), "Put");
                }
                if (passive) {
                    checked(MPI_Win_flush(target, win), "remote flush");
                }
            }
            if (passive) {
                checked(MPI_Barrier(MPI_COMM_WORLD), "after remote flush");
                checked(MPI_Win_sync(win), "consume remote stores");
            } else {
                checked(MPI_Win_fence(0, win), "closing fence");
            }
            int good = verify(exposed, offset, size, rank_id == target,
                              generation, round, "exposed");
            good &= verify(local, offset, size, rank_id == origin,
                           generation, round, "local");
            consensus(good);
            /* No rank reinitializes a window before its peer finishes checking. */
            checked(MPI_Barrier(MPI_COMM_WORLD), "after data and guards");
            printf("MPI_RMA_ROUND rank=%d generation=%u round=%u op=%s "
                   "bytes=%zu origin=%d sync=%s DATA=PASS GUARDS=PASS\n",
                   rank_id, generation, round, get ? "GET" : "PUT", size, origin, sync);
        }
        if (passive) {
            checked(MPI_Win_unlock_all(win), "Win_unlock_all");
        } else {
            checked(MPI_Win_fence(MPI_MODE_NOSUCCEED, win), "close fence epoch");
        }
        checked(MPI_Win_free(&win), "Win_free");
    }
    free(local);
    free(exposed);
    printf("MPI_RMA_DATA_PASS rank=%d sync=%s ROUNDS=32 WINDOWS=2\n", rank_id, sync);
}

/* Separate atomics gate: existing UCP AM emulation is eligible, but must be
 * demonstrated in actual UCX logs. No RM hardware atomic flags are enabled. */
static void atomic_rounds(void)
{
    struct state {
        uint64_t before;
        uint32_t value32;
        uint32_t padding;
        uint64_t value64;
        uint64_t after;
    } *memory;
    MPI_Win win;
    checked(MPI_Win_allocate(sizeof(*memory), 1, MPI_INFO_NULL,
                            MPI_COMM_WORLD, &memory, &win), "atomic Win_allocate");
    memory->before = UINT64_C(0xa5a5a5a5a5a5a5a5);
    memory->after = memory->before;
    memory->padding = UINT32_C(0xa5a5a5a5);
    memory->value32 = 100;
    memory->value64 = 1000;
    checked(MPI_Win_lock_all(0, win), "atomic lock_all");
    checked(MPI_Win_sync(win), "atomic publish");
    checked(MPI_Barrier(MPI_COMM_WORLD), "atomic ready");
    int good = 1;
    if (rank_id == 0) {
        uint32_t add32 = 7, old32 = 0, compare32 = 107, swap32 = 501;
        uint64_t add64 = 9, old64 = 0, compare64 = 1009, swap64 = 9001;
        MPI_Aint d32 = (char *)&memory->value32 - (char *)memory;
        MPI_Aint d64 = (char *)&memory->value64 - (char *)memory;
        checked(MPI_Fetch_and_op(&add32, &old32, MPI_UINT32_T, 1, d32, MPI_SUM, win), "FAA32");
        checked(MPI_Win_flush(1, win), "FAA32 flush");
        good &= old32 == 100;
        checked(MPI_Fetch_and_op(&add64, &old64, MPI_UINT64_T, 1, d64, MPI_SUM, win), "FAA64");
        checked(MPI_Win_flush(1, win), "FAA64 flush");
        good &= old64 == 1000;
        checked(MPI_Compare_and_swap(&swap32, &compare32, &old32, MPI_UINT32_T,
                                    1, d32, win), "CAS32 hit");
        checked(MPI_Win_flush(1, win), "CAS32 hit flush");
        good &= old32 == compare32;
        checked(MPI_Compare_and_swap(&swap64, &compare64, &old64, MPI_UINT64_T,
                                    1, d64, win), "CAS64 hit");
        checked(MPI_Win_flush(1, win), "CAS64 hit flush");
        good &= old64 == compare64;
        checked(MPI_Compare_and_swap(&add32, &compare32, &old32, MPI_UINT32_T,
                                    1, d32, win), "CAS32 miss");
        checked(MPI_Win_flush(1, win), "CAS32 miss flush");
        good &= old32 == swap32;
        checked(MPI_Compare_and_swap(&add64, &compare64, &old64, MPI_UINT64_T,
                                    1, d64, win), "CAS64 miss");
        checked(MPI_Win_flush(1, win), "CAS64 miss flush");
        good &= old64 == swap64;
    }
    checked(MPI_Barrier(MPI_COMM_WORLD), "atomic completed");
    checked(MPI_Win_sync(win), "atomic consume");
    good &= memory->value32 == (rank_id == 1 ? 501u : 100u);
    good &= memory->value64 == (rank_id == 1 ? UINT64_C(9001) : UINT64_C(1000));
    good &= memory->padding == UINT32_C(0xa5a5a5a5);
    good &= memory->before == UINT64_C(0xa5a5a5a5a5a5a5a5);
    good &= memory->after == memory->before;
    consensus(good);
    checked(MPI_Win_unlock_all(win), "atomic unlock_all");
    checked(MPI_Win_free(&win), "atomic Win_free");
    printf("MPI_RMA_AMO_PASS rank=%d FAA32=PASS FAA64=PASS CAS32=PASS "
           "CAS64=PASS CAS_MISS=PASS GUARDS=PASS\n", rank_id);
}

int main(int argc, char **argv)
{
    int count;
    checked(MPI_Init(&argc, &argv), "Init");
    checked(MPI_Comm_rank(MPI_COMM_WORLD, &rank_id), "rank");
    checked(MPI_Comm_size(MPI_COMM_WORLD, &count), "size");
    checked(MPI_Comm_set_errhandler(MPI_COMM_WORLD, MPI_ERRORS_RETURN), "comm errors");
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (count != 2 || argc != 2 ||
        (strcmp(argv[1], "fence") && strcmp(argv[1], "lock_all") && strcmp(argv[1], "atomics"))) {
        fprintf(stderr, "MPI_RMA_ERROR: exactly two ranks and fence|lock_all|atomics required\n");
        MPI_Abort(MPI_COMM_WORLD, 2);
        return 2;
    }
    if (!strcmp(argv[1], "atomics")) {
        atomic_rounds();
    } else {
        data_rounds(!strcmp(argv[1], "lock_all"));
    }
    checked(MPI_Finalize(), "Finalize");
    return 0;
}
