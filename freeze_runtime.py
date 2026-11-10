"""Copy the local execution code into a fresh, versioned run directory; no device access."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil


def freeze(destination,source=None):
    source=Path(source or Path(__file__).parent).resolve(strict=True)
    destination=Path(destination).resolve()
    if destination.exists():raise FileExistsError('Use a fresh runtime directory')
    files=sorted(p for p in source.glob('*.py') if not p.name.startswith('test_'))
    files+=sorted(p for p in (source/'skill/scripts').iterdir() if p.is_file() and p.suffix in ('.py','.ps1'))
    if not files or not (source/'training_worker.py').is_file():raise ValueError('Expected the training runtime source')
    destination.mkdir(parents=True)
    hashes={}
    for path in files:
        relative=path.relative_to(source);target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        hashes[relative.as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'sha256':hashes}
    (destination/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return {'runtime':str(destination),'files':len(hashes),'device_contacted':False}


def verify(source):
    source=Path(source).resolve(strict=True)
    path=source/'runtime-manifest.json'
    if not path.is_file():return
    for name,digest in json.loads(path.read_text(encoding='utf-8'))['sha256'].items():
        target=(source/name).resolve(strict=True)
        if not target.is_relative_to(source) or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
            raise ValueError('Frozen runtime changed; create a fresh reviewed snapshot')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path)
    print(json.dumps(freeze(parser.parse_args().destination)))
