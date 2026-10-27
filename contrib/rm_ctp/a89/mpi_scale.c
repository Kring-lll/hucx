#define _POSIX_C_SOURCE 200809L
#include <mpi.h>
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

/* TCP is a test-only rendezvous, outside every measured communication phase.
 * No MPI barrier/reduction silently creates data EPs before first contact. */
static int world_rank;
static int world_size;
static int ppn;
static int control_fd = -1;
static int *clients;
static unsigned phase_number;

static double now_seconds(void)
{
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return value.tv_sec + value.tv_nsec * 1e-9;
}

static void fail(const char *message)
{
    fprintf(stderr, "A89_DATA_OR_RUNTIME_ERROR rank=%d %s errno=%d\n",
            world_rank, message, errno);
    fflush(stderr);
    /* The launcher owns the job. Do not attempt global machine cleanup. */
    MPI_Abort(MPI_COMM_WORLD, 2);
    exit(2);
}

static void mpi_check(int status)
{
    if (status != MPI_SUCCESS) {
        fail("MPI returned an error");
    }
}

static void transfer(int fd, void *buffer, size_t length, int sending)
{
    unsigned char *position = buffer;
    while (length != 0) {
        ssize_t count = sending ? send(fd, position, length, MSG_NOSIGNAL) :
                                  recv(fd, position, length, 0);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            fail("out-of-band control socket failed or timed out");
        }
        length -= (size_t)count;
        position += count;
    }
}

static void socket_timeout(int fd, int seconds)
{
    struct timeval value = {seconds, 0};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &value, sizeof(value)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &value, sizeof(value)) != 0) {
        fail("socket timeout setup");
    }
}

static void control_open(const char *host, int port, int timeout)
{
    struct sockaddr_in address;
    int fd;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &address.sin_addr) != 1) {
        fail("control address must be an IPv4 literal");
    }
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        fail("control socket");
    }
    socket_timeout(fd, timeout);
    if (world_rank == 0) {
        int reuse = 1;
        unsigned char *seen = calloc((size_t)world_size, 1);
        clients = calloc((size_t)world_size, sizeof(*clients));
        if (clients == NULL || seen == NULL) {
            fail("control clients allocation");
        }
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
        if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
            listen(fd, world_size) != 0) {
            fail("control bind/listen (port may be occupied)");
        }
        for (int index = 1; index < world_size; ++index) {
            uint32_t identity[2];
            int client = accept(fd, NULL, NULL);
            if (client < 0) {
                fail("control accept");
            }
            socket_timeout(client, timeout);
            transfer(client, identity, sizeof(identity), 0);
            unsigned rank = ntohl(identity[0]);
            if (rank == 0 || rank >= (unsigned)world_size || seen[rank] ||
                ntohl(identity[1]) != (unsigned)world_size) {
                fail("duplicate/invalid control rank");
            }
            clients[rank] = client;
            seen[rank] = 1;
        }
        free(seen);
        close(fd);
    } else {
        double deadline = now_seconds() + timeout;
        for (;;) {
            if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) {
                break;
            }
            if (now_seconds() >= deadline) {
                fail("control connect timeout");
            }
            close(fd);
            struct timespec delay = {0, 10000000};
            nanosleep(&delay, NULL);
            fd = socket(AF_INET, SOCK_STREAM, 0);
            if (fd < 0) {
                fail("control retry socket");
            }
            socket_timeout(fd, timeout);
        }
        uint32_t identity[2] = {htonl((uint32_t)world_rank), htonl((uint32_t)world_size)};
        transfer(fd, identity, sizeof(identity), 1);
        control_fd = fd;
    }
}

static void barrier(void)
{
    uint32_t token = htonl(++phase_number);
    if (world_rank == 0) {
        for (int rank = 1; rank < world_size; ++rank) {
            uint32_t received;
            transfer(clients[rank], &received, sizeof(received), 0);
            if (received != token) {
                fail("control phase mismatch");
            }
        }
        for (int rank = 1; rank < world_size; ++rank) {
            transfer(clients[rank], &token, sizeof(token), 1);
        }
    } else {
        uint32_t received;
        transfer(control_fd, &token, sizeof(token), 1);
        transfer(control_fd, &received, sizeof(received), 0);
        if (received != token) {
            fail("control release mismatch");
        }
    }
}

static void fill(unsigned char *buffer, size_t bytes, int sender, uint64_t sequence)
{
    for (size_t index = 0; index < bytes; ++index) {
        buffer[index] = (unsigned char)((sender * 37U + sequence * 17U +
                                       index * 13U + (sequence >> 8)) & 255U);
    }
}

static void verify(const unsigned char *buffer, size_t bytes, int sender,
                   uint64_t sequence)
{
    for (size_t index = 0; index < bytes; ++index) {
        unsigned char expected = (unsigned char)((sender * 37U + sequence * 17U +
                                                 index * 13U + (sequence >> 8)) & 255U);
        if (buffer[index] != expected) {
            fail("payload mismatch");
        }
    }
}

static int partner(int step)
{
    int local = world_rank % ppn;
    return world_rank < ppn ? ppn + (local + step) % ppn :
                              (local - step + ppn) % ppn;
}

static double exchange(int sweeps, uint64_t base)
{
    unsigned char send_buffer[8];
    unsigned char receive_buffer[8];
    double started = now_seconds();
    for (int sweep = 0; sweep < sweeps; ++sweep) {
        for (int step = 0; step < ppn; ++step) {
            int peer = partner(step);
            uint64_t sequence = base + (uint64_t)sweep * ppn + step;
            fill(send_buffer, sizeof(send_buffer), world_rank, sequence);
            mpi_check(MPI_Sendrecv(send_buffer, 8, MPI_BYTE, peer, 41,
                                   receive_buffer, 8, MPI_BYTE, peer, 41,
                                   MPI_COMM_WORLD, MPI_STATUS_IGNORE));
            verify(receive_buffer, sizeof(receive_buffer), peer, sequence);
        }
    }
    return now_seconds() - started;
}

static double concurrent(int rounds, int bytes, uint64_t base)
{
    size_t total = (size_t)ppn * (size_t)bytes;
    unsigned char *send_buffer = malloc(total);
    unsigned char *receive_buffer = malloc(total);
    MPI_Request *requests = malloc((size_t)ppn * 2 * sizeof(*requests));
    if (send_buffer == NULL || receive_buffer == NULL || requests == NULL) {
        fail("message allocation");
    }
    double started = now_seconds();
    for (int round = 0; round < rounds; ++round) {
        uint64_t sequence = base + (uint64_t)round;
        for (int local = 0; local < ppn; ++local) {
            int peer = world_rank < ppn ? ppn + local : local;
            fill(send_buffer + (size_t)local * bytes, (size_t)bytes, world_rank, sequence);
            mpi_check(MPI_Irecv(receive_buffer + (size_t)local * bytes, bytes,
                                MPI_BYTE, peer, 42, MPI_COMM_WORLD, &requests[local]));
        }
        for (int local = 0; local < ppn; ++local) {
            int peer = world_rank < ppn ? ppn + local : local;
            mpi_check(MPI_Isend(send_buffer + (size_t)local * bytes, bytes,
                                MPI_BYTE, peer, 42, MPI_COMM_WORLD, &requests[ppn + local]));
        }
        mpi_check(MPI_Waitall(2 * ppn, requests, MPI_STATUSES_IGNORE));
        for (int local = 0; local < ppn; ++local) {
            int peer = world_rank < ppn ? ppn + local : local;
            verify(receive_buffer + (size_t)local * bytes, (size_t)bytes, peer, sequence);
        }
    }
    double elapsed = now_seconds() - started;
    free(requests);
    free(receive_buffer);
    free(send_buffer);
    return elapsed;
}

static long rss_kib(void)
{
    char line[256];
    FILE *stream = fopen("/proc/self/status", "r");
    long result = -1;
    if (stream == NULL) {
        fail("open /proc/self/status");
    }
    while (fgets(line, sizeof(line), stream) != NULL) {
        if (sscanf(line, "VmRSS: %ld kB", &result) == 1) {
            break;
        }
    }
    fclose(stream);
    if (result < 0) {
        fail("read RSS");
    }
    return result;
}

static void snapshot(const char *directory, const char *phase)
{
    char filename[4096];
    char buffer[8192];
    snprintf(filename, sizeof(filename), "%s/rank%d_%s.maps", directory, world_rank, phase);
    FILE *source = fopen("/proc/self/maps", "r");
    FILE *target = fopen(filename, "w");
    if (source == NULL || target == NULL) {
        fail("maps snapshot open");
    }
    size_t count;
    while ((count = fread(buffer, 1, sizeof(buffer), source)) != 0) {
        if (fwrite(buffer, 1, count, target) != count) {
            fail("maps snapshot write");
        }
    }
    if (ferror(source) || fclose(target) != 0) {
        fail("maps snapshot flush");
    }
    fclose(source);
}

int main(int argc, char **argv)
{
    double started = now_seconds();
    mpi_check(MPI_Init(&argc, &argv));
    double init_seconds = now_seconds() - started;
    mpi_check(MPI_Comm_rank(MPI_COMM_WORLD, &world_rank));
    mpi_check(MPI_Comm_size(MPI_COMM_WORLD, &world_size));
    mpi_check(MPI_Comm_set_errhandler(MPI_COMM_WORLD, MPI_ERRORS_RETURN));
    /* ppn root-ip port output-dir rounds timeout hostname0 hostname1 */
    if (argc != 9) {
        fail("expected 8 arguments");
    }
    ppn = atoi(argv[1]);
    int port = atoi(argv[3]);
    int rounds = atoi(argv[5]);
    int timeout = atoi(argv[6]);
    if (ppn < 1 || ppn > 192 || world_size != 2 * ppn || rounds < 1 ||
        port < 1024 || port > 65535 || timeout < 5) {
        fail("invalid size/rounds/control parameters");
    }
    char hostname[256] = {0};
    if (gethostname(hostname, sizeof(hostname) - 1) != 0 ||
        strcmp(hostname, argv[world_rank < ppn ? 7 : 8]) != 0) {
        fail("rank order is not the expected two-node block placement");
    }
    const char *local = getenv("OMPI_COMM_WORLD_LOCAL_RANK");
    if (local == NULL || atoi(local) != world_rank % ppn) {
        fail("local rank placement mismatch");
    }
    long init_rss = rss_kib();
    snapshot(argv[4], "after_init");
    control_open(argv[2], port, timeout);
    barrier();
    double first = exchange(1, 0);
    barrier();
    long first_rss = rss_kib();
    snapshot(argv[4], "after_first");
    barrier();
    (void)exchange(2, 1000);
    barrier();
    double steady = exchange(rounds, 10000);
    barrier();
    double burst64 = concurrent(rounds, 64, 1000000);
    barrier();
    double burst1024 = concurrent(rounds, 1024, 2000000);
    barrier();
    long steady_rss = rss_kib();
    snapshot(argv[4], "after_steady");
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage) != 0) {
        fail("getrusage");
    }
    barrier();
    if (world_rank == 0) {
        for (int rank = 1; rank < world_size; ++rank) {
            close(clients[rank]);
        }
        free(clients);
    } else {
        close(control_fd);
    }
    started = now_seconds();
    mpi_check(MPI_Finalize());
    double finalize_seconds = now_seconds() - started;
    char filename[4096];
    snprintf(filename, sizeof(filename), "%s/rank%d.json", argv[4], world_rank);
    FILE *result = fopen(filename, "w");
    if (result == NULL) {
        perror("result file");
        return 2;
    }
    fprintf(result,
        "{\"rank\":%d,\"ppn\":%d,\"hostname\":\"%s\",\"pid\":%ld,"
        "\"init_s\":%.9f,\"first_all_peers_s\":%.9f,\"steady_s\":%.9f,"
        "\"burst64_s\":%.9f,\"burst1024_s\":%.9f,\"finalize_s\":%.9f,"
        "\"rounds\":%d,\"init_rss_kib\":%ld,\"first_rss_kib\":%ld,"
        "\"steady_rss_kib\":%ld,\"peak_rss_kib\":%ld,"
        "\"data_validation\":true,\"finalized\":true}\n",
        world_rank, ppn, hostname, (long)getpid(), init_seconds, first, steady,
        burst64, burst1024, finalize_seconds, rounds, init_rss, first_rss,
        steady_rss, usage.ru_maxrss);
    if (fclose(result) != 0) {
        perror("result flush");
        return 2;
    }
    printf("A89_RANK_PASS rank=%d ppn=%d host=%s finalized=YES\n", world_rank, ppn, hostname);
    return 0;
}
