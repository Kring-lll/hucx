/* One-way burst: no application ACK until all 16 messages arrive.
 * With FC window 8, independent credit grants are required for progress. */
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static void require(int ok) { if (!ok) MPI_Abort(MPI_COMM_WORLD, 21); }
static unsigned char pattern(int peer, unsigned seq, size_t off)
{ return (unsigned char)(peer * 17u + seq * 29u + off * 13u); }
static void record(int rank, int maps)
{
    char path[4096], key[128], line[4096];
    const char *dir = getenv("R1_MAP_DIR"), *kind = getenv("R2_FC_KIND");
    const char *names[] = {"FC_ENABLE", "FC_WND_SIZE", "TX_DEPTH", "RX_DEPTH"};
    const char *values[] = {"y", "8", "8", "64"};
    FILE *in, *out;
    unsigned i;
    require(dir && kind && (!strcmp(kind, "RM") || !strcmp(kind, "RC")));
    if (!maps) {
        snprintf(path, sizeof(path), "%s/rank%d.fc.env", dir, rank);
        out = fopen(path, "w"); require(out != NULL);
        for (i = 0; i < 4; ++i) {
            const char *value;
            snprintf(key, sizeof(key), "UCX_%s_URMA_%s", kind, names[i]);
            value = getenv(key); require(value && !strcmp(value, values[i]));
            fprintf(out, "%s=%s\n", key, value);
        }
        fclose(out); return;
    }
    snprintf(path, sizeof(path), "%s/rank%d_pid%ld.maps", dir, rank, (long)getpid());
    in = fopen("/proc/self/maps", "r"); out = fopen(path, "w");
    require(in && out);
    while (fgets(line, sizeof(line), in))
        if (strstr(line, "libucp") || strstr(line, "libuct") ||
            strstr(line, "libucs") || strstr(line, "libucm") || strstr(line, "liburma"))
            fputs(line, out);
    fclose(in); fclose(out);
}
int main(int argc, char **argv)
{
    int rank, size, peer, pos;
    unsigned seq, batch, rounds = 2000, burst = 16;
    size_t bytes, off, i;
    unsigned char *tx, *rx;
    unsigned long long ack[2];
    MPI_Request *req;
    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank); MPI_Comm_size(MPI_COMM_WORLD, &size);
    require(argc == 3); bytes = strtoul(argv[1], NULL, 10);
    rounds = strtoul(argv[2], NULL, 10);
    require(size >= 2 && bytes >= 8 && bytes <= 2048 && rounds > 0);
    tx = malloc(bytes * (size_t)size * burst); rx = malloc(bytes);
    req = malloc((size_t)size * burst * sizeof(*req)); require(tx && rx && req);
    record(rank, 0);
    for (seq = 0; seq < rounds; ++seq) {
        if (rank == 0) {
            pos = 0;
            for (batch = 0; batch < burst; ++batch)
                for (peer = 1; peer < size; ++peer) {
                    off = ((size_t)batch * size + peer) * bytes;
                    for (i = 0; i < bytes; ++i)
                        tx[off + i] = pattern(peer, seq * burst + batch, i);
                    MPI_Isend(tx + off, (int)bytes, MPI_BYTE, peer, 10,
                              MPI_COMM_WORLD, &req[pos++]);
                }
            MPI_Waitall(pos, req, MPI_STATUSES_IGNORE);
            for (peer = 1; peer < size; ++peer) {
                MPI_Recv(ack, sizeof(ack), MPI_BYTE, peer, 11,
                         MPI_COMM_WORLD, MPI_STATUS_IGNORE);
                require(ack[0] == (unsigned)peer && ack[1] == seq);
            }
        } else {
            for (batch = 0; batch < burst; ++batch) {
                MPI_Recv(rx, (int)bytes, MPI_BYTE, 0, 10, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
                for (i = 0; i < bytes; ++i)
                    require(rx[i] == pattern(rank, seq * burst + batch, i));
            }
            ack[0] = (unsigned)rank; ack[1] = seq;
            MPI_Send(ack, sizeof(ack), MPI_BYTE, 0, 11, MPI_COMM_WORLD);
        }
    }
    record(rank, 1); free(req); free(rx); free(tx);
    if (rank == 0) {
        printf("R2_FC_ONEWAY_PASS peers=%d bytes=%zu rounds=%u burst=%u\n",
               size - 1, bytes, rounds, burst); fflush(stdout);
    }
    MPI_Finalize(); return 0;
}
