/* Test-only file-backed API harness. This is NOT an MPI implementation,
 * transport mock, performance model, or proof of URMA behavior. */
#ifndef A89_CPU_MPI_H
#define A89_CPU_MPI_H
#include <stddef.h>
typedef int MPI_Comm;
typedef int MPI_Datatype;
typedef int MPI_Errhandler;
typedef struct { void *buffer; int count; int peer; int tag; int receive; } MPI_Request;
typedef int MPI_Status;
#define MPI_COMM_WORLD 0
#define MPI_ERRORS_RETURN 1
#define MPI_SUCCESS 0
#define MPI_BYTE 0
#define MPI_STATUS_IGNORE ((MPI_Status *)0)
#define MPI_STATUSES_IGNORE ((MPI_Status *)0)
int MPI_Init(int *, char ***);
int MPI_Comm_rank(MPI_Comm, int *);
int MPI_Comm_size(MPI_Comm, int *);
int MPI_Comm_set_errhandler(MPI_Comm, MPI_Errhandler);
int MPI_Abort(MPI_Comm, int);
int MPI_Sendrecv(const void *, int, MPI_Datatype, int, int, void *, int,
                 MPI_Datatype, int, int, MPI_Comm, MPI_Status *);
int MPI_Irecv(void *, int, MPI_Datatype, int, int, MPI_Comm, MPI_Request *);
int MPI_Isend(const void *, int, MPI_Datatype, int, int, MPI_Comm, MPI_Request *);
int MPI_Waitall(int, MPI_Request *, MPI_Status *);
int MPI_Finalize(void);
#endif
