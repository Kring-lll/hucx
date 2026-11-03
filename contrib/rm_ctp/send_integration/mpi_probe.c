/* Test application only: deterministic SEND data, ordering and full guards. */
#define _GNU_SOURCE
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define GUARD 64u
#define MAX_BYTES 1048576u
#define DATA_TAG 113
static const size_t normal_sizes[] = {
    8, 3981, 8, 3982, 64, 7938, 8, 7939, 117, 65536, 8, 1048576, 8
};
static const size_t injected_sizes[] = {8, 65536, 8};

static void fail(int rank, const char *what, int code)
{
    fprintf(stderr, "SEND_MPI_FAIL rank=%d what=%s code=%d\n", rank, what, code);
    fflush(stderr);
    MPI_Abort(MPI_COMM_WORLD, code);
    exit(code);
}

static unsigned char value_for(int source, unsigned sequence, size_t offset)
{
    if (offset < 4) {
        return (unsigned char)((unsigned)source >> (8 * offset));
    }
    if (offset < 8) {
        return (unsigned char)(sequence >> (8 * (offset - 4)));
    }
    return (unsigned char)((source * 37u + sequence * 19u + offset * 13u + 0x5b) & 0xff);
}

static void fill(unsigned char *p, size_t n, int source, unsigned sequence)
{
    memset(p, 0xa5, GUARD);
    memset(p + GUARD + n, 0x5a, MAX_BYTES - n + GUARD);
    for (size_t i = 0; i < n; ++i) {
        p[GUARD + i] = value_for(source, sequence, i);
    }
}

static int check(const unsigned char *p, size_t n, int source, unsigned sequence)
{
    for (size_t i = 0; i < GUARD; ++i) {
        if (p[i] != 0xa5) {
            return 1;
        }
    }
    for (size_t i = n; i < MAX_BYTES + GUARD; ++i) {
        if (p[GUARD + i] != 0x5a) {
            return 2;
        }
    }
    for (size_t i = 0; i < n; ++i) {
        if (p[GUARD + i] != value_for(source, sequence, i)) {
            return 3;
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    int rank = -1;
    int world = 0;
    int injection = argc == 2 && strcmp(argv[1], "--injection") == 0;
    unsigned cycles = injection ? 1 : 2;
    const size_t *sizes = injection ? injected_sizes : normal_sizes;
    size_t count_sizes = injection ? sizeof(injected_sizes) / sizeof(injected_sizes[0]) :
                                    sizeof(normal_sizes) / sizeof(normal_sizes[0]);
    unsigned sent = 0;
    unsigned received = 0;
    unsigned sequence = 0;
    size_t sent_bytes = 0;
    size_t received_bytes = 0;
    size_t expected_bytes = 0;
    if (MPI_Init(&argc, &argv) != MPI_SUCCESS) {
        return 70;
    }
    if (MPI_Comm_rank(MPI_COMM_WORLD, &rank) != MPI_SUCCESS ||
        MPI_Comm_size(MPI_COMM_WORLD, &world) != MPI_SUCCESS) {
        fail(rank, "identity", 71);
    }
    if (world < 2 || world > 8 || world % 2 || (argc != 1 && !injection)) {
        fail(rank, "world_or_arguments", 72);
    }
    const char *directory = getenv("HUCX_OSU_PROBE_DIR");
    const char *nonce = getenv("A89_RUN_NONCE");
    if (directory == NULL || nonce == NULL || strlen(nonce) != 32) {
        fail(rank, "proof_directory_or_nonce", 73);
    }
    unsigned char *sendbuf = malloc(MAX_BYTES + 2 * GUARD);
    unsigned char *recvbuf = malloc(MAX_BYTES + 2 * GUARD);
    if (sendbuf == NULL || recvbuf == NULL) {
        fail(rank, "allocation", 74);
    }
    int half = world / 2;
    int lower = rank < half ? rank : rank - half;
    int upper = lower + half;
    int peer = rank == lower ? upper : lower;
    for (size_t s = 0; s < count_sizes; ++s) {
        expected_bytes += cycles * sizes[s];
    }
    for (int direction = 0; direction < 2; ++direction) {
        int sender = direction == 0 ? lower : upper;
        int receiver = direction == 0 ? upper : lower;
        for (unsigned cycle = 0; cycle < cycles; ++cycle) {
            for (size_t s = 0; s < count_sizes; ++s, ++sequence) {
                size_t n = sizes[s];
                if (rank == sender) {
                    fill(sendbuf, n, rank, sequence);
                    if (MPI_Send(sendbuf + GUARD, (int)n, MPI_BYTE, receiver, DATA_TAG,
                                 MPI_COMM_WORLD) != MPI_SUCCESS) {
                        fail(rank, "send", 75);
                    }
                    if (check(sendbuf, n, rank, sequence)) {
                        fail(rank, "source_changed", 76);
                    }
                    ++sent;
                    sent_bytes += n;
                } else if (rank == receiver) {
                    MPI_Status status;
                    int actual_count = -1;
                    memset(recvbuf, 0xa5, GUARD);
                    memset(recvbuf + GUARD, 0xcd, n);
                    memset(recvbuf + GUARD + n, 0x5a, MAX_BYTES - n + GUARD);
                    /* Same tag for every message: sequence checks cannot be hidden
                     * by matching a different MPI tag for each expected sequence. */
                    if (MPI_Recv(recvbuf + GUARD, (int)n, MPI_BYTE, sender, DATA_TAG,
                                 MPI_COMM_WORLD, &status) != MPI_SUCCESS ||
                        MPI_Get_count(&status, MPI_BYTE, &actual_count) != MPI_SUCCESS) {
                        fail(rank, "recv", 77);
                    }
                    if (status.MPI_SOURCE != sender || status.MPI_TAG != DATA_TAG ||
                        actual_count != (int)n || check(recvbuf, n, sender, sequence)) {
                        fail(rank, "content_order_full_guard", 78);
                    }
                    ++received;
                    received_bytes += n;
                } else {
                    fail(rank, "pair_role", 79);
                }
            }
        }
        if (MPI_Barrier(MPI_COMM_WORLD) != MPI_SUCCESS) {
            fail(rank, "phase_barrier", 80);
        }
        /* Check unexpected application data after both sides finish this phase.
         * This is a bounded check, not a claim about arbitrarily late duplicates. */
        int pending = 0;
        MPI_Status status;
        if (MPI_Iprobe(MPI_ANY_SOURCE, MPI_ANY_TAG, MPI_COMM_WORLD,
                       &pending, &status) != MPI_SUCCESS || pending) {
            fail(rank, "unexpected_or_duplicate_data", 81);
        }
        /* Keep the next direction from starting during another rank's check. */
        if (MPI_Barrier(MPI_COMM_WORLD) != MPI_SUCCESS) {
            fail(rank, "check_barrier", 87);
        }
    }
    unsigned expected = cycles * (unsigned)count_sizes;
    if (sent != expected || received != expected || sent_bytes != expected_bytes ||
        received_bytes != expected_bytes) {
        fail(rank, "message_or_byte_count", 82);
    }
    free(sendbuf);
    free(recvbuf);
    if (MPI_Finalize() != MPI_SUCCESS) {
        return 83;
    }
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/mpi_rank%d.json", directory, rank) >= (int)sizeof(path)) {
        return 84;
    }
    FILE *proof = fopen(path, "wx");
    if (proof == NULL) {
        return 85;
    }
    fprintf(proof, "{\"rank\":%d,\"world\":%d,\"peer\":%d,\"pid\":%ld,\"nonce\":\"%s\","
                   "\"sent\":%u,\"received\":%u,\"send_bytes\":%zu,\"receive_bytes\":%zu,"
                   "\"cycles\":%u,\"injection\":%s,\"content\":\"PASS\",\"order\":\"PASS\","
                   "\"guards\":\"PASS\",\"unexpected_messages\":0,\"finalized\":true,\"sizes\":[",
            rank, world, peer, (long)getpid(), nonce, sent, received, sent_bytes,
            received_bytes, cycles, injection ? "true" : "false");
    for (size_t s = 0; s < count_sizes; ++s) {
        fprintf(proof, "%s%zu", s ? "," : "", sizes[s]);
    }
    fprintf(proof, "]}\n");
    if (fclose(proof) != 0) {
        return 86;
    }
    printf("SEND_MPI_PASS rank=%d sent=%u received=%u finalized=YES\n", rank, sent, received);
    return 0;
}
