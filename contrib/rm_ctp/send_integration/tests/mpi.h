#ifndef SEND_INTEGRATION_MPI_STUB_H
#define SEND_INTEGRATION_MPI_STUB_H
typedef int MPI_Comm;
typedef int MPI_Datatype;
typedef struct { int MPI_SOURCE; int MPI_TAG; int MPI_ERROR; } MPI_Status;
#define MPI_COMM_WORLD 0
#define MPI_BYTE 1
#define MPI_ANY_SOURCE -1
#define MPI_ANY_TAG -1
#define MPI_SUCCESS 0
int MPI_Init(int *, char ***);
int MPI_Finalize(void);
int MPI_Abort(MPI_Comm, int);
int MPI_Comm_rank(MPI_Comm, int *);
int MPI_Comm_size(MPI_Comm, int *);
int MPI_Send(const void *, int, MPI_Datatype, int, int, MPI_Comm);
int MPI_Recv(void *, int, MPI_Datatype, int, int, MPI_Comm, MPI_Status *);
int MPI_Get_count(const MPI_Status *, MPI_Datatype, int *);
int MPI_Iprobe(int, int, MPI_Comm, int *, MPI_Status *);
int MPI_Barrier(MPI_Comm);
#endif
