"""Package the original 0.3 logic as an upgrade from candidate versionCode 6."""
from pathlib import Path
import os
import struct
import subprocess
import zipfile


def bump_manifest(source,expected=3,version=7):
    if not expected<version<2147483648:raise ValueError('Rollback must be an upgrade')
    data=bytearray(source)
    if len(data)<8 or struct.unpack_from('<HHI',data)!=(3,8,len(data)):raise ValueError('Invalid binary XML header')
    position=8;ids=[];changed=[]
    while position<len(data):
        if position+8>len(data):raise ValueError('Truncated XML chunk')
        kind,header,size=struct.unpack_from('<HHI',data,position)
        if not 8<=header<=size or position+size>len(data):raise ValueError('Invalid XML chunk extent')
        if kind==0x180:
            if (size-header)%4:raise ValueError('Invalid resource map')
            ids=list(struct.unpack_from('<'+'I'*((size-header)//4),data,position+header))
        if kind==0x102:
            if header!=16 or size<36:raise ValueError('Invalid element header')
            start,stride,count=struct.unpack_from('<HHH',data,position+24)
            if start<20 or stride<20 or 16+start+stride*count>size:raise ValueError('Invalid attribute extent')
            for index in range(count):
                at=position+16+start+index*stride
                name,raw=struct.unpack_from('<II',data,at+4)
                if name<len(ids) and ids[name]==0x0101021b:
                    value_size,reserved,value_type,value=struct.unpack_from('<HBBI',data,at+12)
                    if (value_size,reserved,value_type,value,raw)!=(8,0,0x10,expected,0xffffffff):raise ValueError('Unexpected original versionCode encoding')
                    struct.pack_into('<I',data,at+16,version);changed.append(at+16)
        position+=size
    if len(changed)!=1:raise ValueError('Expected one android:versionCode attribute')
    return bytes(data)


def main():
    root=Path(__file__).resolve().parent
    original=root/'ucampus-bridge-test.apk';output=root/'ucampus-bridge-rollback-v7.apk'
    build=root/'build';build.mkdir(exist_ok=True)
    unsigned=build/'rollback-unsigned.apk';aligned=build/'rollback-aligned.apk'
    with zipfile.ZipFile(original) as source,zipfile.ZipFile(unsigned,'w') as target:
        for item in source.infolist():
            data=source.read(item.filename)
            if item.filename=='AndroidManifest.xml':data=bump_manifest(data)
            target.writestr(item,data)
    sdk=Path(os.environ['LOCALAPPDATA'])/'Android/Sdk/build-tools/36.0.0'
    subprocess.run([str(sdk/'zipalign.exe'),'-f','4',str(unsigned),str(aligned)],check=True)
    subprocess.run([str(sdk/'apksigner.bat'),'sign','--ks',str(root/'test-signing.p12'),'--ks-key-alias','local-test',
                    '--ks-pass','pass:android','--key-pass','pass:android','--out',str(output),str(aligned)],check=True)
    subprocess.run([str(sdk/'apksigner.bat'),'verify','--verbose',str(output)],check=True)
    with zipfile.ZipFile(original) as before,zipfile.ZipFile(output) as after:
        for name in before.namelist():
            if name.upper()=='META-INF/MANIFEST.MF' or (name.startswith('META-INF/') and name.upper().endswith(('.SF','.RSA','.DSA','.EC'))):continue
            if name!='AndroidManifest.xml' and before.read(name)!=after.read(name):raise RuntimeError('Rollback changed original content: '+name)
    print('Original DEX and resources preserved; only manifest versionCode changed 3 -> 7: '+str(output))


if __name__=='__main__':main()
