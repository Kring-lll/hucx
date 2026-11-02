/* Diagnostic-only correlation: MPI data call -> public UCP NBI -> rkey trace.
 * No interposition library from this file is loaded in timing rounds. */
#define _GNU_SOURCE
#include <mpi.h>
#include <ucp/api/ucp.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static _Thread_local const char *active_operation;
static _Thread_local int active_target;

static void *resolve(const char *symbol)
{
    void *address = dlsym(RTLD_NEXT, symbol);
    if (address == NULL) {
        fprintf(stderr, "MPI_RMA_ERROR: diagnostic symbol unavailable: %s\n", symbol);
        _exit(97);
    }
    return address;
}

static void record(const char *operation, size_t length, ucp_rkey_h key,
                   ucs_status_t status)
{
    const char *directory = getenv("HUCX_OSU_PROBE_DIR");
    const char *rank = getenv("OMPI_COMM_WORLD_RANK");
    char path[PATH_MAX];
    if (active_operation == NULL) {
        return;
    }
    if (directory == NULL || rank == NULL ||
        snprintf(path, sizeof(path), "%s/rank%s_ucp_path.jsonl", directory, rank) >= (int)sizeof(path)) {
        fprintf(stderr, "MPI_RMA_ERROR: diagnostic output path\n");
        _exit(97);
    }
    FILE *stream = fopen(path, "a");
    if (stream == NULL) {
        fprintf(stderr, "MPI_RMA_ERROR: diagnostic output open\n");
        _exit(97);
    }
    fprintf(stream, "{\"rank\":%d,\"pid\":%ld,\"target\":%d,\"mpi_op\":\"%s\","
                    "\"ucp_op\":\"%s\",\"length\":%zu,\"rkey\":\"%p\",\"status\":%d}\n",
            atoi(rank), (long)getpid(), active_target, active_operation,
            operation, length, (void *)key, (int)status);
    if (fclose(stream) != 0) {
        fprintf(stderr, "MPI_RMA_ERROR: diagnostic output close\n");
        _exit(97);
    }
}

int MPI_Put(const void *origin_addr, int origin_count, MPI_Datatype origin_type,
            int target, MPI_Aint displacement, int target_count,
            MPI_Datatype target_type, MPI_Win window)
{
    active_operation = "PUT";
    active_target = target;
    int result = PMPI_Put(origin_addr, origin_count, origin_type, target,
                          displacement, target_count, target_type, window);
    active_operation = NULL;
    return result;
}

int MPI_Get(void *origin_addr, int origin_count, MPI_Datatype origin_type,
            int target, MPI_Aint displacement, int target_count,
            MPI_Datatype target_type, MPI_Win window)
{
    active_operation = "GET";
    active_target = target;
    int result = PMPI_Get(origin_addr, origin_count, origin_type, target,
                          displacement, target_count, target_type, window);
    active_operation = NULL;
    return result;
}

ucs_status_t ucp_put_nbi(ucp_ep_h ep, const void *buffer, size_t length,
                       uint64_t remote_address, ucp_rkey_h key)
{
    typedef ucs_status_t (*function_t)(ucp_ep_h, const void *, size_t, uint64_t, ucp_rkey_h);
    function_t function = (function_t)resolve("ucp_put_nbi");
    ucs_status_t status = function(ep, buffer, length, remote_address, key);
    record("put", length, key, status);
    return status;
}

ucs_status_t ucp_get_nbi(ucp_ep_h ep, void *buffer, size_t length,
                       uint64_t remote_address, ucp_rkey_h key)
{
    typedef ucs_status_t (*function_t)(ucp_ep_h, void *, size_t, uint64_t, ucp_rkey_h);
    function_t function = (function_t)resolve("ucp_get_nbi");
    ucs_status_t status = function(ep, buffer, length, remote_address, key);
    record("get", length, key, status);
    return status;
}
