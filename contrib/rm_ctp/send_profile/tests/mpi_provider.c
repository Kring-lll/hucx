int MPI_Init(int *argc,char ***argv){(void)argc;(void)argv;return 0;}
int MPI_Init_thread(int *argc,char ***argv,int required,int *provided)
{(void)argc;(void)argv;*provided=required;return 0;}
int MPI_Finalize(void){return 0;}
