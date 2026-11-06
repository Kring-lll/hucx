"""Public-SDK forwarding tests, including RTLD_LOCAL; no URMA device use."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('sdk',type=Path,nargs='?')
    parser.add_argument('--include',type=Path)
    args=parser.parse_args()
    include=args.include or args.sdk/'include'
    root=Path(__file__).resolve().parent
    count=0
    with tempfile.TemporaryDirectory() as temp:
        out=Path(temp)
        builds=[('liburma.so',['-shared','-fPIC'],root/'wire_fixture.c',[]),
            ('libwire.so',['-shared','-fPIC'],root/'wire_probe.c',['-ldl','-lpthread']),
            ('client',['-DWIRE_CLIENT'],root/'wire_fixture.c',['-L'+str(out),'-lurma','-Wl,-rpath,'+str(out)]),
            ('local_client',['-DWIRE_LOCAL_CLIENT'],root/'wire_fixture.c',['-ldl'])]
        for name,extra,source,libs in builds:
            subprocess.run(['gcc','-O2','-g','-Wall','-Wextra','-Werror','-I'+str(include),
                *extra,str(source),*libs,'-o',str(out/name)],check=True)
        shutil.copy2(out/'liburma.so',out/'liburma.so.0')
        shutil.copy2(out/'liburma.so',out/'libunrelated.so')
        for scope in ('global','local','local_versioned'):
            for header in (8,6):
                for error in (False,True):
                    run=out/str(count);run.mkdir();count+=1
                    env={k:v for k,v in os.environ.items() if k not in ('LD_PRELOAD','LD_LIBRARY_PATH','FAKE_ERROR')}
                    env.update(LD_PRELOAD=str(out/'libwire.so'),HUCX_NOPSN_WIRE_DIR=str(run),
                        A89_RUN_NONCE='c'*32,OMPI_COMM_WORLD_RANK='0')
                    if error:env['FAKE_ERROR']='1'
                    command=[str(out/('client' if scope=='global' else 'local_client')),str(header)]
                    if scope!='global':command.append(str(out/('liburma.so.0' if scope=='local_versioned' else 'liburma.so')))
                    subprocess.run(command,env=env,check=True)
                    receipts=list(run.glob('nopsn_wire*.json'));assert len(receipts)==1
                    row=json.loads(receipts[0].read_text())
                    assert row['local_handle_used']==int(scope!='global') and row['provider_resolved'],row
                    assert row['errors']==0 and row['retry_success']==0,row
                    if error:
                        assert row['data_success']==row['control_success']==row['header_mask']==0,row
                    else:
                        assert row['header_mask']==1<<header and row['short_success']==row['bcopy_success']==row['control_success']==1 and row['data_success']==2,row
                        assert row['max_wr_bytes']==header+4088,row
        for provider in ('missing',str(out/'libunrelated.so')):
            run=out/str(count);run.mkdir();count+=1
            env=dict(os.environ,LD_PRELOAD=str(out/'libwire.so'),HUCX_NOPSN_WIRE_DIR=str(run),
                A89_RUN_NONCE='c'*32,OMPI_COMM_WORLD_RANK='0')
            env.pop('LD_LIBRARY_PATH',None);env.pop('FAKE_ERROR',None)
            result=subprocess.run([str(out/'local_client'),'6',provider],env=env,capture_output=True,text=True)
            assert result.returncode==121 and 'NOPSN_WIRE_RESOLVE_ERROR' in result.stderr,result
            assert not list(run.glob('nopsn_wire*.json'))
    print('WIRE_PUBLIC_SDK_GLOBAL_LOCAL_VERSIONED_ERRORS_MISSING=%d/%d PASS CPU_ONLY=YES'%(count,count))


if __name__=='__main__':main()
