"""Execute the actual guard/pattern program with a file API harness, not MPI/URMA."""
import os
import sys
from pathlib import Path
import subprocess
import tempfile
root=Path(__file__).resolve().parent

def main():
 with tempfile.TemporaryDirectory() as tmp:
  out=Path(tmp);fixture=root.parent/'a89/tests/cpu_mpi'
  (out/'mpi.h').write_text((fixture/'mpi.h').read_text().replace('#endif','int MPI_Get_count(const MPI_Status *, MPI_Datatype, int *);\n#endif'))
  text=(fixture/'file_mpi.c').read_text().replace('[384][64]','[384][512]')
  text=text.replace('(void)stype; (void)rtype; (void)comm; (void)status;','(void)stype; (void)rtype; (void)comm; if(status)*status=rcount;')
  text=text.replace('int MPI_Finalize(void) { return 0; }','int MPI_Finalize(void) { return getenv("CPU_MPI_FAIL_FINALIZE") ? 9 : 0; }')
  text+='\nint MPI_Get_count(const MPI_Status *s,MPI_Datatype t,int *n){(void)t;*n=*s;return 0;}\n'
  (out/'file_mpi.c').write_text(text)
  subprocess.run(['gcc','-std=c99','-O2','-Wall','-Wextra','-Werror','-I'+str(out),str(root/'alternating.c'),str(out/'file_mpi.c'),'-o',str(out/'alternate')],check=True)
  for size,corrupt,finalize_error in ((2,False,False),(4,False,False),(2,True,False),(2,False,True)):
   path=out/('run%d_%s_%s'%(size,corrupt,finalize_error));path.mkdir();processes=[]
   for rank in range(size):
    env=dict(os.environ,CPU_MPI_RANK=str(rank),CPU_MPI_SIZE=str(size),CPU_MPI_DIR=str(path))
    if corrupt:env['CPU_MPI_CORRUPT']='1'
    if finalize_error:env['CPU_MPI_FAIL_FINALIZE']='1'
    processes.append(subprocess.Popen([str(out/'alternate')],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE))
   outputs=[]
   try:
    for p in processes:
     stdout,stderr=p.communicate(timeout=45);outputs.append((p.returncode,stdout,stderr))
   finally:
    for p in processes:
     if p.poll() is None:p.kill();p.wait()
   if corrupt:assert any(v[0]==4 for v in outputs),outputs
   elif finalize_error:
    assert all(v[0]==8 for v in outputs) and not any('NOPSN_ALTERNATING_PASS' in v[1] for v in outputs),outputs
   else:
    assert all(v[0]==0 for v in outputs),outputs
    assert sum('NOPSN_ALTERNATING_PASS' in v[1] for v in outputs)==1,outputs
    sys.path.insert(0,str(root))
    from run_candidate import parse_alternating
    parse_alternating('\n'.join(v[1]+'\n'+v[2] for v in outputs),size//2)
 print('ALTERNATING_PROGRAM_FILE_HARNESS_2_4_RANKS_CORRUPTION_AND_FINALIZE_ERROR_REJECT=PASS')

if __name__=='__main__':main()
