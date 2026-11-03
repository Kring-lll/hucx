/* CPU test adapter only. File queues exercise the real probe's C control flow.
 * This is NOT an MPI implementation/transport or a substitute for HMPI tests. */
#define _GNU_SOURCE
#include "mpi.h"
#include <glob.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int rank_id;
static int world;
static unsigned outgoing[8];
static unsigned incoming[8];
static unsigned barrier_id;
static const char *directory;

static void path_for(char *path, const char *kind, int source, int dest, unsigned seq)
{
    snprintf(path, 4096, "%s/%s_%d_%d_%u", directory, kind, source, dest, seq);
}

static FILE *wait_file(const char *path)
{
    for (unsigned i = 0; i < 15000; ++i) {
        FILE *file = fopen(path, "rb");
        if (file != NULL) {
            return file;
        }
        usleep(1000);
    }
    fprintf(stderr, "CPU_ADAPTER_TIMEOUT %s\n", path);
    exit(98);
}

int MPI_Init(int *argc, char ***argv)
{
    (void)argc;
    (void)argv;
    rank_id = atoi(getenv("TEST_RANK"));
    world = atoi(getenv("TEST_WORLD"));
    directory = getenv("TEST_QUEUE");
    return 0;
}
int MPI_Comm_rank(MPI_Comm comm, int *rank) { (void)comm; *rank = rank_id; return 0; }
int MPI_Comm_size(MPI_Comm comm, int *size) { (void)comm; *size = world; return 0; }
int MPI_Abort(MPI_Comm comm, int code) { (void)comm; exit(code); }

int MPI_Send(const void *buffer, int count, MPI_Datatype type, int dest, int tag, MPI_Comm comm)
{
    (void)type;
    (void)comm;
    char path[4096];
    char temporary[4096];
    unsigned seq = outgoing[dest]++;
    path_for(path, "message", rank_id, dest, seq);
    path_for(temporary, "writing", rank_id, dest, seq);
    FILE *file = fopen(temporary, "wb");
    if (file == NULL) exit(97);
    fwrite(&count, sizeof(count), 1, file);
    fwrite(&tag, sizeof(tag), 1, file);
    unsigned char *copy = malloc((size_t)count);
    memcpy(copy, buffer, (size_t)count);
    if (seq == 0 && getenv("TEST_CORRUPT_RANK") && rank_id == atoi(getenv("TEST_CORRUPT_RANK"))) {
        copy[0] ^= 1;
    }
    fwrite(copy, 1, (size_t)count, file);
    free(copy);
    if (fclose(file) || rename(temporary, path)) exit(97);
    return 0;
}

int MPI_Recv(void *buffer, int capacity, MPI_Datatype type, int source, int tag, MPI_Comm comm, MPI_Status *status)
{
    (void)type;
    (void)comm;
    char path[4096];
    int count;
    int actual_tag;
    path_for(path, "message", source, rank_id, incoming[source]++);
    FILE *file = wait_file(path);
    if (fread(&count, sizeof(count), 1, file) != 1 ||
        fread(&actual_tag, sizeof(actual_tag), 1, file) != 1 || count > capacity || actual_tag != tag) exit(96);
    if (fread(buffer, 1, (size_t)count, file) != (size_t)count) exit(96);
    fclose(file);
    unlink(path);
    if (getenv("TEST_FAR_GUARD_RANK") && rank_id == atoi(getenv("TEST_FAR_GUARD_RANK")) && count == 8) {
        ((unsigned char *)buffer)[count + 70] ^= 1;
    }
    status->MPI_SOURCE = source;
    status->MPI_TAG = actual_tag;
    status->MPI_ERROR = count;
    return 0;
}
int MPI_Get_count(const MPI_Status *status, MPI_Datatype type, int *count)
{
    (void)type;
    *count = status->MPI_ERROR;
    return 0;
}
int MPI_Barrier(MPI_Comm comm)
{
    (void)comm;
    char path[4096];
    unsigned step = barrier_id++;
    path_for(path, "barrier", rank_id, 0, step);
    FILE *file = fopen(path, "wb");
    if (file == NULL) exit(95);
    fclose(file);
    for (int rank = 0; rank < world; ++rank) {
        path_for(path, "barrier", rank, 0, step);
        file = wait_file(path);
        fclose(file);
    }
    return 0;
}
int MPI_Iprobe(int source, int tag, MPI_Comm comm, int *flag, MPI_Status *status)
{
    (void)source;
    (void)tag;
    (void)comm;
    (void)status;
    char pattern[4096];
    glob_t paths;
    snprintf(pattern, sizeof(pattern), "%s/message_*_%d_*", directory, rank_id);
    int code = glob(pattern, 0, NULL, &paths);
    *flag = code == 0 && paths.gl_pathc > 0;
    globfree(&paths);
    return 0;
}
int MPI_Finalize(void) { return MPI_Barrier(MPI_COMM_WORLD); }
