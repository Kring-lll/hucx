"""Compile public-SDK forwarding fixture; no URMA device use."""
import json,os
from pathlib import Path
import subprocess,sys,tempfile
def main():
 root=Path(__file__).resolve().parent
 sdk=Path(sys.argv[1]).resolve()
 with tempfile.TemporaryDirectory() as temp:
  out=Path(temp)
  for name,extra,source,libs in [('libprovider.so',['-shared','-fPIC'],root/'wire_fixture.c',[]),('libwire.so',['-shared','-fPIC','-Wall','-Wextra','-Werror'],root/'wire_probe.c',['-ldl','-lpthread']),('client',['-DWIRE_CLIENT'],root/'wire_fixture.c',['-L'+str(out),'-lprovider','-Wl,-rpath,'+str(out)])]:
   subprocess.run(['gcc','-O2','-g','-I'+str(sdk/'include'),*extra,str(source),*libs,'-o',str(out/name)],check=True)
  for header in (8,6):
   run=out/str(header);run.mkdir();env=dict(os.environ,LD_PRELOAD=str(out/'libwire.so'),HUCX_NOPSN_WIRE_DIR=str(run),A89_RUN_NONCE='c'*32,OMPI_COMM_WORLD_RANK='0')
   subprocess.run([str(out/'client'),str(header)],env=env,check=True)
   receipts=list(run.glob('nopsn_wire*.json'));assert len(receipts)==1
   row=json.loads(receipts[0].read_text())
   assert row['header_mask']==1<<header and row['errors']==0 and row['short_success']==row['bcopy_success']==row['control_success']==1 and row['data_success']==2 and row['retry_success']==0,row
   assert row['max_wr_bytes']==header+4088,row
  print('WIRE_PUBLIC_SDK_FORWARDING_HEADER8_AND6_RECEIPTS=PASS')

if __name__=='__main__':main()
