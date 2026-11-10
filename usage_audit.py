"""Normalize Codex and DSH v4 token metadata in explicit time windows; never output conversation content."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path

FIELDS=('input_tokens','cached_input_tokens','output_tokens','total_tokens')
DSH_FIELDS=('inputTokens','cacheReadTokens','cacheWriteTokens','outputTokens','totalTokens')


def counters(block,fields=FIELDS):
    if not isinstance(block,dict) or not set(fields)<=block.keys():
        raise ValueError('Missing required token counters')
    row={key:block[key] for key in fields}
    if any(type(value) is not int or value<0 for value in row.values()):
        raise ValueError('Invalid token counter')
    if fields==FIELDS:
        if row['cached_input_tokens']>row['input_tokens']:raise ValueError('Cache exceeds input')
        if row['input_tokens']+row['output_tokens']!=row['total_tokens']:
            raise ValueError('Inconsistent token total; refusing a partial estimate')
    return row


def timestamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Use timestamps with an explicit timezone')
    return result


def audit(lines,start,end):
    if start.tzinfo is None or end.tzinfo is None:raise ValueError('Use timestamps with an explicit timezone')
    if start>=end:raise ValueError('Start must precede end')
    previous=schema=None
    result={'usage_updates':0,**{key:0 for key in FIELDS}}
    for line in lines:
        if '"token_count"' not in line and '"assistant/message"' not in line:continue
        event=json.loads(line)
        if not isinstance(event,dict):continue
        payload=event.get('payload',{})
        if event.get('type')=='event_msg' and isinstance(payload,dict) and payload.get('type')=='token_count':
            info=payload.get('info')
            if info is None:continue
            current='codex'
        elif event.get('type')=='assistant/message':
            data=event.get('data')
            if not isinstance(data,dict):raise ValueError('Missing DSH message metadata')
            usage=data.get('usage')
            if usage is None:continue
            current='dsh-v4'
        else:continue
        if schema is not None and schema!=current:
            raise ValueError('Do not mix Codex and DSH usage records in one log')
        schema=current
        if schema=='codex':
            if not isinstance(info,dict):raise ValueError('Missing Codex usage metadata')
            total=counters(info.get('total_token_usage'))
            if total==previous:continue
            last=counters(info.get('last_token_usage'))
            delta={key:total[key]-previous[key] for key in FIELDS} if previous is not None else last
            previous=total
            if any(value<0 for value in delta.values()) or delta!=last:
                raise ValueError('Counter reset or missing usage events; audit an intact segment')
            moment=timestamp(event['timestamp'])
        else:
            millis=event.get('time')
            if type(millis) is not int or millis<0:raise ValueError('Invalid DSH millisecond timestamp')
            moment=datetime.fromtimestamp(millis/1000,tz=timezone.utc)
            if not start<=moment<end:continue
            raw=counters(usage,DSH_FIELDS)
            # shortcut: verify a real cache-write fixture before supporting that accounting convention.
            if raw['cacheWriteTokens']:raise ValueError('Nonzero DSH cacheWriteTokens accounting is not supported')
            delta=counters({'input_tokens':raw['inputTokens']+raw['cacheReadTokens'],
                            'cached_input_tokens':raw['cacheReadTokens'],'output_tokens':raw['outputTokens'],
                            'total_tokens':raw['totalTokens']})
        if start<=moment<end:
            result['usage_updates']+=1
            for key in FIELDS:result[key]+=delta[key]
    if schema is None:raise ValueError('No supported token usage records found; zero usage was not established')
    result['uncached_input_tokens']=result['input_tokens']-result['cached_input_tokens']
    result['schema']=schema
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs',type=Path,nargs='+')
    parser.add_argument('--start',type=timestamp,required=True)
    parser.add_argument('--end',type=timestamp,required=True)
    args=parser.parse_args()
    logs=[p.resolve(strict=True) for p in args.logs]
    if len(set(logs))!=len(logs):parser.error('Do not count the same log twice')
    totals={key:0 for key in ('usage_updates',*FIELDS,'uncached_input_tokens')};schemas=set()
    for path in logs:
        with path.open(encoding='utf-8') as stream:row=audit(stream,args.start,args.end)
        schemas.add(row['schema'])
        for key in totals:totals[key]+=row[key]
    print(json.dumps({'start':args.start.isoformat(),'end_exclusive':args.end.isoformat(),
                      'log_count':len(logs),'log_schemas':sorted(schemas),**totals,'money_estimated':False}))


if __name__=='__main__':main()
