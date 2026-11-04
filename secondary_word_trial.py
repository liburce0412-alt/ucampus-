"""Record one inspected Pre word on an already running, bounded secondary display."""
import argparse
import importlib.util
import json
from pathlib import Path
from project_paths import SKILL
import sys

from audio_injection_trial import word_turn
from screen_frames import ScreenFrames,display_adb
from training_session import Session


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wav',type=Path)
    parser.add_argument('--word',required=True)
    parser.add_argument('--xy',type=int,nargs=2,required=True)
    parser.add_argument('--display-id',type=int,required=True)
    parser.add_argument('--scrcpy',type=Path)
    parser.add_argument('--capture-display-id',type=int)
    parser.add_argument('--port',type=int,default=8794)
    parser.add_argument('--skill',type=Path,default=SKILL)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.display_id<=0 or args.output.exists():parser.error('Use an existing secondary display and fresh evidence directory')
    if not args.scrcpy and args.capture_display_id is None:parser.error('An explicit secondary capture is required')
    scripts=args.skill.resolve(strict=True)/'scripts';sys.path.insert(0,str(scripts))
    import runtime
    if ':' not in runtime.serial():raise ValueError('Use the authorized wireless device')
    spec=importlib.util.spec_from_file_location('control',scripts/'ucampus-control.py')
    control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
    session=Session(control,args.port,args.display_id)
    frames=None
    args.output.mkdir(parents=True)
    result={'display_id':args.display_id,'capture_display_id':args.capture_display_id,'digital_injection':False}
    try:
        session.check_audio(args.wav)
        session.client.observe(full=True)
        if args.capture_display_id is None:
            frames=ScreenFrames(runtime.adb_path(),runtime.serial(),args.scrcpy,args.display_id)
            frames.start()
        control.adb=display_adb(control.adb,session.client,frames,args.capture_display_id)
        result.update(word_turn(session,args.wav,args.word,*args.xy))
    except Exception as error:
        result.update(error=str(error),next='Inspect this display before retrying; do not target the main screen')
        raise
    finally:
        try:
            if frames:frames.close()
        finally:
            session.client.close()
            (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
