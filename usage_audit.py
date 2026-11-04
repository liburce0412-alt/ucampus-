"""Count token metadata in explicit local session logs; never output conversation content."""
import argparse
from datetime import datetime
import json
from pathlib import Path

FIELDS=('input_tokens','cached_input_tokens','output_tokens','total_tokens')


def timestamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Use timestamps with an explicit timezone')
    return result


def audit(lines,start,end):
    if start>=end:raise ValueError('Start must precede end')
    previous=None
    result={'usage_updates':0,**{key:0 for key in FIELDS}}
    for line in lines:
        if '"token_count"' not in line:continue
        event=json.loads(line)
        payload=event.get('payload',{})
        if event.get('type')!='event_msg' or payload.get('type')!='token_count' or not payload.get('info'):continue
        info=payload['info']
        total={key:info['total_token_usage'][key] for key in FIELDS}
        if any(type(v) is not int or v<0 for v in total.values()):raise ValueError('Invalid token counter')
        if total==previous:continue
        last={key:info['last_token_usage'][key] for key in FIELDS}
        delta={key:total[key]-previous[key] for key in FIELDS} if previous is not None else last
        previous=total
        if any(type(v) is not int or v<0 for v in delta.values()) or delta!=last:
            raise ValueError('Counter reset or missing usage events; audit an intact segment')
        if delta['cached_input_tokens']>delta['input_tokens']:raise ValueError('Cache exceeds input')
        if start<=timestamp(event['timestamp'])<end:
            result['usage_updates']+=1
            for key in FIELDS:result[key]+=delta[key]
    result['uncached_input_tokens']=result['input_tokens']-result['cached_input_tokens']
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs',type=Path,nargs='+')
    parser.add_argument('--start',type=timestamp,required=True)
    parser.add_argument('--end',type=timestamp,required=True)
    args=parser.parse_args()
    logs=[p.resolve(strict=True) for p in args.logs]
    if len(set(logs))!=len(logs):parser.error('Do not count the same log twice')
    totals={}
    for path in logs:
        with path.open(encoding='utf-8') as stream:row=audit(stream,args.start,args.end)
        for key,value in row.items():totals[key]=totals.get(key,0)+value
    print(json.dumps({'start':args.start.isoformat(),'end_exclusive':args.end.isoformat(),
                      'log_count':len(logs),**totals,'money_estimated':False}))


if __name__=='__main__':main()
