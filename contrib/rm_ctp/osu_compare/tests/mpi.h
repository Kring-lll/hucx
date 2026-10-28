/* CPU loader fixture only, not an MPI implementation. */
#ifndef OSU_PROBE_CPU_MPI_H
#define OSU_PROBE_CPU_MPI_H
typedef int MPI_Comm;
#define MPI_COMM_WORLD 0
#define MPI_SUCCESS 0
int MPI_Init(int *, char ***);
int MPI_Init_thread(int *, char ***, int, int *);
int MPI_Finalize(void);
int PMPI_Init(int *, char ***);
int PMPI_Init_thread(int *, char ***, int, int *);
int PMPI_Finalize(void);
int PMPI_Comm_rank(MPI_Comm, int *);
int PMPI_Comm_size(MPI_Comm, int *);
#endif
