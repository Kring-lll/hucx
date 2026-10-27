/* Diagnostic only: no collective or point-to-point call before the first round. */
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int rank_id;

static void stage(const char *name)
{
    fprintf(stderr, "MPI_PHASE RANK=%d PID=%ld STAGE=%s\n", rank_id, (long)getpid(), name);
    fflush(stderr);
}

static void check(int code)
{
    if (code != MPI_SUCCESS) {
        fprintf(stderr, "MPI_PHASE_FAILED RANK=%d CODE=%d\n", rank_id, code);
        exit(2);
    }
}

static void save_maps(const char *directory)
{
    char path[4096];
    char line[4096];
    FILE *source;
    FILE *destination;
    int length = snprintf(path, sizeof(path), "%s/rank%d.maps", directory, rank_id);
    if (length <= 0 || (size_t)length >= sizeof(path)) {
        exit(2);
    }
    source = fopen("/proc/self/maps", "r");
    destination = fopen(path, "w");
    if (source == NULL || destination == NULL) {
        exit(2);
    }
    while (fgets(line, sizeof(line), source) != NULL) {
        if (strstr(line, "libucp") || strstr(line, "libuct") ||
            strstr(line, "libucs") || strstr(line, "libucm") ||
            strstr(line, "libmpi") || strstr(line, "liburma")) {
            fputs(line, destination);
        }
    }
    fclose(source);
    fclose(destination);
}

int main(int argc, char **argv)
{
    const char *rank_text = getenv("OMPI_COMM_WORLD_RANK");
    uint64_t payload;
    uint64_t expected;
    int size;
    int round;
    rank_id = rank_text == NULL ? -1 : atoi(rank_text);
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc != 2) {
        return 2;
    }
    stage("BEFORE_MPI_INIT");
    check(MPI_Init(&argc, &argv));
    stage("AFTER_MPI_INIT");
    check(MPI_Comm_rank(MPI_COMM_WORLD, &rank_id));
    check(MPI_Comm_size(MPI_COMM_WORLD, &size));
    if (size != 2 || rank_id < 0 || rank_id > 1) {
        MPI_Abort(MPI_COMM_WORLD, 2);
        return 2;
    }
    for (round = 0; round < 2; ++round) {
        stage(round == 0 ? "BEFORE_FIRST_DATA" : "BEFORE_SECOND_DATA");
        expected = UINT64_C(0x724d435450000000) + (unsigned)round;
        if (rank_id == 0) {
            payload = expected;
            check(MPI_Send(&payload, 1, MPI_UINT64_T, 1, 20 + round, MPI_COMM_WORLD));
            check(MPI_Recv(&payload, 1, MPI_UINT64_T, 1, 30 + round,
                           MPI_COMM_WORLD, MPI_STATUS_IGNORE));
        } else {
            check(MPI_Recv(&payload, 1, MPI_UINT64_T, 0, 20 + round,
                           MPI_COMM_WORLD, MPI_STATUS_IGNORE));
            if (payload != expected) {
                MPI_Abort(MPI_COMM_WORLD, 3);
                return 3;
            }
            payload ^= UINT64_C(0x0102030405060708);
            check(MPI_Send(&payload, 1, MPI_UINT64_T, 0, 30 + round, MPI_COMM_WORLD));
        }
        if (rank_id == 0 && payload != (expected ^ UINT64_C(0x0102030405060708))) {
            MPI_Abort(MPI_COMM_WORLD, 3);
            return 3;
        }
        stage(round == 0 ? "AFTER_FIRST_DATA" : "AFTER_SECOND_DATA");
    }
    save_maps(argv[1]);
    stage("BEFORE_MPI_FINALIZE");
    check(MPI_Finalize());
    stage("AFTER_MPI_FINALIZE");
    fprintf(stderr, "MPI_PHASE_PASS RANK=%d ROUNDS=2 BYTES=8 VALIDATION=PASS\n", rank_id);
    return 0;
}
