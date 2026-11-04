"""Bounded secondary-display read test; run only at an idle U-campus home/stage boundary."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from bridge_client import Client,BridgeError


def scrcpy_args(executable,serial,video):
    return [str(executable),'-s',serial,'--new-display=1440x3200','--no-vd-destroy-content',
            '--display-ime-policy=local','--start-app=cn.unipus.cloud','--no-audio','--no-window',
            '--max-fps=5','--video-bit-rate=2M','--time-limit=18','--record='+str(video)]


def return_state(client):
    windows=client.rpc('windows')
    result={'after_windows':windows,
            'secondary_window_remaining':any(row['display_id']!=0 for row in windows['windows']),
            'display_device_release':'requires_display_list_check',
            'training_state_preserved':'requires_app_readback'}
    try:
        result['main_snapshot']=client.observe(full=True)
        result['main_ucampus_foreground']=True
    except BridgeError as error:
        if str(error)!='target_not_foreground':raise
        # Background training must not bring U-campus in front of the user's main-screen app.
        result['main_ucampus_foreground']=False
    return result


def run(args):
    if args.output.exists():raise ValueError('Use a fresh evidence directory')
    client=Client(args.port)
    try:
        if not client.rpc('ping').get('display_selection'):raise RuntimeError('Install and verify the 0.4 display helper first')
        before=client.observe(full=True)
        before_windows=client.rpc('windows')
    finally:client.close()
    args.output.mkdir(parents=True)
    (args.output/'before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
    result={'before_windows':before_windows,'audio_tested':False,'recording_tested':False,'ui_actions_sent':0}
    process=None
    started=time.monotonic()
    with (args.output/'scrcpy.log').open('wb') as log:
        try:
            process=subprocess.Popen(scrcpy_args(args.scrcpy,args.serial,args.output/'display.mkv'),
                                     stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+14
            display=None
            while time.monotonic()<deadline and process.poll() is None:
                windows=Client(args.port)
                try:found=windows.rpc('windows')['windows']
                finally:windows.close()
                candidates={row['display_id'] for row in found if row['display_id']!=0 and row['focused']}
                if len(candidates)==1:
                    display=candidates.pop()
                    break
                if len(candidates)>1:raise RuntimeError('Ambiguous U-campus secondary display')
                time.sleep(.3)
            if display is None:raise RuntimeError('No focused U-campus secondary display was observed')
            client=Client(args.port,display_id=display)
            try:result['secondary_snapshot']=client.observe(full=True)
            finally:client.close()
            result['display_id']=display
            result['secondary_read_verified']=True
            process.wait(timeout=max(1,24-(time.monotonic()-started)))
        except Exception as error:
            result['error']=type(error).__name__+': '+str(error)
        finally:
            if process is not None and process.poll() is None:
                # Let the short time limit close the display and migrate its activities back.
                try:process.wait(timeout=max(1,26-(time.monotonic()-started)))
                except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
            result['elapsed_seconds']=round(time.monotonic()-started,3)
            client=Client(args.port)
            try:
                result.update(return_state(client))
            except Exception as error:result['restore_check_error']=type(error).__name__+': '+str(error)
            finally:client.close()
            (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--scrcpy',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8794)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args),ensure_ascii=False))
