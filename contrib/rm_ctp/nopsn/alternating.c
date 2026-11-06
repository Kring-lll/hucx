/* Correctness supplement only; not presented as an OSU performance result. */
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define GUARD 64
#define CAP 1048576
static unsigned char pattern(unsigned rank,unsigned round,unsigned i)
{ return (unsigned char)(rank*17u+round*13u+i+(i>>8)*7u+(i>>16)*11u); }
int main(int argc,char **argv)
{
    static const int sizes[]={8,64,104,105,4079,4080,4081,8135,8136,8137,65536,CAP,8};
    int rank,ranks,source,target;
    unsigned char *a=malloc(CAP+2*GUARD),*b=malloc(CAP+2*GUARD);
    MPI_Init(&argc,&argv);MPI_Comm_rank(MPI_COMM_WORLD,&rank);MPI_Comm_size(MPI_COMM_WORLD,&ranks);
    if(!a || !b || ranks<2) MPI_Abort(MPI_COMM_WORLD,2);
    source=(rank+ranks-1)%ranks;target=(rank+1)%ranks;
    for(int round=0;round<260;++round) {
        int n=sizes[round%(sizeof(sizes)/sizeof(sizes[0]))],got;
        MPI_Status status;
        if(n<0 || n>CAP) { MPI_Abort(MPI_COMM_WORLD,7); return 7; }
        memset(a,0xa5,CAP+2*GUARD);memset(b,0xa5,CAP+2*GUARD);
        for(int i=0;i<n;++i) a[GUARD+i]=pattern(rank,round,i);
        memset(b+GUARD,0xcc,n);
        MPI_Sendrecv(a+GUARD,n,MPI_BYTE,target,round,b+GUARD,n,MPI_BYTE,source,round,MPI_COMM_WORLD,&status);
        MPI_Get_count(&status,MPI_BYTE,&got);
        if(got!=n) MPI_Abort(MPI_COMM_WORLD,3);
        for(int i=0;i<n;++i) if(a[GUARD+i]!=pattern(rank,round,i) || b[GUARD+i]!=pattern(source,round,i)) MPI_Abort(MPI_COMM_WORLD,4);
        for(int i=0;i<GUARD;++i) if(a[i]!=0xa5 || b[i]!=0xa5) MPI_Abort(MPI_COMM_WORLD,5);
        for(int i=GUARD+n;i<CAP+2*GUARD;++i) if(a[i]!=0xa5 || b[i]!=0xa5) MPI_Abort(MPI_COMM_WORLD,6);
    }
    fprintf(stderr,"NOPSN_ALTERNATING_DATA_DONE RANK=%d ROUNDS=260\n",rank);
    fflush(stderr);
    free(a);free(b);
    if(MPI_Finalize()!=MPI_SUCCESS) return 8;
    if(rank==0) printf("NOPSN_ALTERNATING_PASS RANKS=%d ROUNDS=260\n",ranks);
    return 0;
}
