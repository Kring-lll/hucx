/* Cross-node star traffic; no intra-node MPI data transfers or MPI collectives.
 * Rank 0 belongs on node89; all other ranks belong on node97. */
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static unsigned char pattern(int peer, unsigned seq, size_t off)
{
    return (unsigned char)(peer * 17u + seq * 29u + off * 13u);
}
static void fill(unsigned char *p, size_t n, int peer, unsigned seq)
{
    size_t i;
    for (i = 0; i < n; ++i) p[i] = pattern(peer, seq, i);
}
static void check(const unsigned char *p, size_t n, int peer, unsigned seq)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        if (p[i] != pattern(peer, seq, i)) {
            fprintf(stderr, "R1_DATA_ERROR peer=%d seq=%u off=%zu\n", peer, seq, i);
            MPI_Abort(MPI_COMM_WORLD, 2);
        }
    }
}
static void maps(int rank)
{
    const char *dir = getenv("R1_MAP_DIR");
    char path[4096], line[4096];
    FILE *in, *out;
    if (!dir) return;
    snprintf(path, sizeof(path), "%s/rank%d_pid%ld.maps", dir, rank, (long)getpid());
    in = fopen("/proc/self/maps", "r"); out = fopen(path, "w");
    if (!in || !out) MPI_Abort(MPI_COMM_WORLD, 3);
    while (fgets(line, sizeof(line), in)) {
        if (strstr(line, "libucp") || strstr(line, "libuct") ||
            strstr(line, "libucs") || strstr(line, "libucm") || strstr(line, "liburma"))
            fputs(line, out);
    }
    fclose(in); fclose(out);
}
int main(int argc, char **argv)
{
    int rank, size, peer;
    unsigned seq, batch, rounds = 2000, burst = 16;
    size_t bytes = 64;
    unsigned char *tx, *rx;
    MPI_Request *req;
    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);
    if (argc > 1) bytes = strtoul(argv[1], NULL, 10);
    if (argc > 2) rounds = strtoul(argv[2], NULL, 10);
    if (size < 2 || bytes < 8 || bytes > 2048 || rounds == 0)
        MPI_Abort(MPI_COMM_WORLD, 4);
    tx = malloc(bytes * (size_t)size * burst);
    rx = malloc(bytes * (size_t)size * burst);
    req = malloc(2 * (size_t)size * burst * sizeof(*req));
    if (!tx || !rx || !req) MPI_Abort(MPI_COMM_WORLD, 5);
    for (seq = 0; seq < rounds; ++seq) {
        if (rank == 0) {
            unsigned pos = 0;
            for (batch = 0; batch < burst; ++batch)
                for (peer = 1; peer < size; ++peer) {
                    size_t off = ((size_t)batch * size + peer) * bytes;
                    fill(tx + off, bytes, peer, seq * burst + batch);
                    MPI_Irecv(rx + off, (int)bytes, MPI_BYTE, peer, 11,
                              MPI_COMM_WORLD, &req[pos++]);
                }
            for (batch = 0; batch < burst; ++batch)
                for (peer = 1; peer < size; ++peer) {
                    size_t off = ((size_t)batch * size + peer) * bytes;
                    MPI_Isend(tx + off, (int)bytes, MPI_BYTE, peer, 10,
                              MPI_COMM_WORLD, &req[pos++]);
                }
            MPI_Waitall((int)pos, req, MPI_STATUSES_IGNORE);
            for (batch = 0; batch < burst; ++batch)
                for (peer = 1; peer < size; ++peer) {
                    size_t off = ((size_t)batch * size + peer) * bytes;
                    check(rx + off, bytes, peer, seq * burst + batch);
                }
        } else {
            for (batch = 0; batch < burst; ++batch) {
                MPI_Recv(rx, (int)bytes, MPI_BYTE, 0, 10, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
                check(rx, bytes, rank, seq * burst + batch);
                MPI_Send(rx, (int)bytes, MPI_BYTE, 0, 11, MPI_COMM_WORLD);
            }
        }
    }
    maps(rank);
    free(req); free(rx); free(tx);
    if (rank == 0) {
        printf("R1_MANY_PEER_PASS peers=%d bytes=%zu rounds=%u burst=%u\n", size - 1, bytes, rounds, burst);
        fflush(stdout);
    }
    MPI_Finalize();
    return 0;
}
