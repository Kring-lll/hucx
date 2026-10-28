/* Metadata only at MPI startup/finalize. No SEND/RECV/collective/Wtime hooks.
 * The original OSU timed loops and output are unchanged. */
#define _GNU_SOURCE
#include <mpi.h>
#include <ctype.h>
#include <limits.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int rank_id = -1;
static int world_size = 0;
static int initialized = 0;
static int proof_error = 0;
static long init_rss = 0;
static const char *directory = NULL;
static char hostname[256];

static long rss_kib(void)
{
    FILE *file = fopen("/proc/self/status", "r");
    char line[256];
    long value = -1;
    if (file == NULL) {
        proof_error = 1;
        return -1;
    }
    while (fgets(line, sizeof(line), file) != NULL) {
        if (sscanf(line, "VmRSS: %ld kB", &value) == 1) {
            break;
        }
    }
    fclose(file);
    return value;
}

static void maps(const char *phase)
{
    char name[PATH_MAX];
    char buffer[8192];
    size_t size;
    if (directory == NULL || snprintf(name, sizeof(name), "%s/rank%d_%s.maps",
                                     directory, rank_id, phase) >= (int)sizeof(name)) {
        proof_error = 1;
        return;
    }
    FILE *source = fopen("/proc/self/maps", "r");
    FILE *target = fopen(name, "w");
    if (source == NULL || target == NULL) {
        proof_error = 1;
    } else {
        while ((size = fread(buffer, 1, sizeof(buffer), source)) != 0) {
            if (fwrite(buffer, 1, size, target) != size) {
                proof_error = 1;
                break;
            }
        }
        if (ferror(source)) {
            proof_error = 1;
        }
    }
    if (source != NULL) {
        fclose(source);
    }
    if (target != NULL && fclose(target) != 0) {
        proof_error = 1;
    }
}

static void after_init(int code)
{
    if (code != MPI_SUCCESS || initialized) {
        return;
    }
    initialized = 1;
    directory = getenv("HUCX_OSU_PROBE_DIR");
    if (PMPI_Comm_rank(MPI_COMM_WORLD, &rank_id) != MPI_SUCCESS ||
        PMPI_Comm_size(MPI_COMM_WORLD, &world_size) != MPI_SUCCESS ||
        gethostname(hostname, sizeof(hostname)) != 0) {
        proof_error = 1;
    }
    hostname[sizeof(hostname) - 1] = '\0';
    for (size_t i = 0; hostname[i] != '\0'; ++i) {
        if (!isalnum((unsigned char)hostname[i]) && hostname[i] != '-' &&
            hostname[i] != '_' && hostname[i] != '.') {
            proof_error = 1;
        }
    }
    const char *rank = getenv("OMPI_COMM_WORLD_RANK");
    if (rank == NULL || atoi(rank) != rank_id || world_size < 2 || world_size % 2 != 0) {
        proof_error = 1;
    }
    init_rss = rss_kib();
    maps("after_init");
}

int MPI_Init(int *argc, char ***argv)
{
    int code = PMPI_Init(argc, argv);
    after_init(code);
    return code;
}

int MPI_Init_thread(int *argc, char ***argv, int required, int *provided)
{
    int code = PMPI_Init_thread(argc, argv, required, provided);
    after_init(code);
    return code;
}

int MPI_Finalize(void)
{
    if (!initialized || directory == NULL) {
        return PMPI_Finalize();
    }
    long final_rss = rss_kib();
    maps("before_finalize");
    cpu_set_t affinity;
    CPU_ZERO(&affinity);
    if (sched_getaffinity(0, sizeof(affinity), &affinity) != 0) {
        proof_error = 1;
    }
    int code = PMPI_Finalize();
    char name[PATH_MAX];
    if (snprintf(name, sizeof(name), "%s/rank%d.json", directory, rank_id) >= (int)sizeof(name)) {
        fprintf(stderr, "OSU_PROOF_ERROR: output path too long\n");
        return code;
    }
    FILE *file = fopen(name, "w");
    if (file == NULL) {
        fprintf(stderr, "OSU_PROOF_ERROR: cannot write rank metadata\n");
        return code;
    }
    fprintf(file, "{\"rank\":%d,\"size\":%d,\"ppn\":%d,\"pid\":%ld,"
                  "\"hostname\":\"%s\",\"init_rss_kib\":%ld,"
                  "\"before_finalize_rss_kib\":%ld,\"proof_error\":%d,"
                  "\"finalized\":%s,\"cpu_affinity\":[",
            rank_id, world_size, world_size / 2, (long)getpid(), hostname,
            init_rss, final_rss, proof_error, code == MPI_SUCCESS ? "true" : "false");
    int first = 1;
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
        if (CPU_ISSET(cpu, &affinity)) {
            fprintf(file, "%s%d", first ? "" : ",", cpu);
            first = 0;
        }
    }
    fprintf(file, "]}\n");
    if (fclose(file) != 0) {
        fprintf(stderr, "OSU_PROOF_ERROR: rank metadata close failed\n");
    }
    return code;
}
