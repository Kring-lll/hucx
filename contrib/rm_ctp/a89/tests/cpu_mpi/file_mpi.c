#define _POSIX_C_SOURCE 200809L
#include "mpi.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static int rank_value;
static int size_value;
static unsigned send_sequence[384][64];
static unsigned recv_sequence[384][64];
static const char *directory;

static void filename(char *out, int sender, int receiver, int tag, unsigned sequence)
{
    snprintf(out, 4096, "%s/msg_%d_%d_%d_%u", directory, sender, receiver, tag, sequence);
}

static int send_data(const void *data, int count, int peer, int tag)
{
    char name[4096];
    char temporary[4128];
    filename(name, rank_value, peer, tag, send_sequence[peer][tag]++);
    snprintf(temporary, sizeof(temporary), "%s.tmp", name);
    FILE *stream = fopen(temporary, "wb");
    if (stream == NULL || fwrite(data, 1, (size_t)count, stream) != (size_t)count ||
        fclose(stream) != 0 || rename(temporary, name) != 0) {
        return 1;
    }
    return 0;
}

static int receive_data(void *data, int count, int peer, int tag)
{
    char name[4096];
    filename(name, peer, rank_value, tag, recv_sequence[peer][tag]++);
    FILE *stream = NULL;
    for (unsigned retry = 0; retry < 10000; ++retry) {
        stream = fopen(name, "rb");
        if (stream != NULL) {
            break;
        }
        struct timespec delay = {0, 1000000};
        nanosleep(&delay, NULL);
    }
    if (stream == NULL || fread(data, 1, (size_t)count, stream) != (size_t)count) {
        return 1;
    }
    fclose(stream);
    unlink(name);
    if (getenv("CPU_MPI_CORRUPT") != NULL && rank_value == 0) {
        ((unsigned char *)data)[0] ^= 1;
    }
    return 0;
}

int MPI_Init(int *argc, char ***argv)
{
    (void)argc;
    (void)argv;
    rank_value = atoi(getenv("CPU_MPI_RANK"));
    size_value = atoi(getenv("CPU_MPI_SIZE"));
    directory = getenv("CPU_MPI_DIR");
    return 0;
}
int MPI_Comm_rank(MPI_Comm comm, int *rank) { (void)comm; *rank = rank_value; return 0; }
int MPI_Comm_size(MPI_Comm comm, int *size) { (void)comm; *size = size_value; return 0; }
int MPI_Comm_set_errhandler(MPI_Comm comm, MPI_Errhandler handler)
{ (void)comm; (void)handler; return 0; }
int MPI_Abort(MPI_Comm comm, int code) { (void)comm; _exit(code); }
int MPI_Sendrecv(const void *send, int scount, MPI_Datatype stype, int peer, int stag,
                 void *recv, int rcount, MPI_Datatype rtype, int source, int rtag,
                 MPI_Comm comm, MPI_Status *status)
{
    (void)stype; (void)rtype; (void)comm; (void)status;
    return send_data(send, scount, peer, stag) || receive_data(recv, rcount, source, rtag);
}
int MPI_Irecv(void *buffer, int count, MPI_Datatype type, int peer, int tag,
              MPI_Comm comm, MPI_Request *request)
{
    (void)type; (void)comm;
    *request = (MPI_Request){buffer, count, peer, tag, 1};
    return 0;
}
int MPI_Isend(const void *buffer, int count, MPI_Datatype type, int peer, int tag,
              MPI_Comm comm, MPI_Request *request)
{
    (void)type; (void)comm;
    *request = (MPI_Request){NULL, 0, 0, 0, 0};
    return send_data(buffer, count, peer, tag);
}
int MPI_Waitall(int count, MPI_Request *requests, MPI_Status *statuses)
{
    (void)statuses;
    for (int index = 0; index < count; ++index) {
        MPI_Request *r = &requests[index];
        if (r->receive && receive_data(r->buffer, r->count, r->peer, r->tag) != 0) {
            return 1;
        }
    }
    return 0;
}
int MPI_Finalize(void) { return 0; }
