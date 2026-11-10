"""Count token metadata in DSH native session logs (schema v4); never output conversation content.

`usage_audit.py` reads the Codex rollout schema (`event_msg` / `token_count`
records).  DSH writes a different schema: every `assistant/message` event
carries its own `data.usage` block with per-request counters.  Pointing
`usage_audit.py` at a v4 log is accepted but reports zero updates, which is
easy to misread as "no tokens were spent".  This module reads the v4 block
and reports the same field names, so both audits stay comparable.

Usage:
    python usage_audit_dsh.py SESSION.jsonl --start 2026-11-01T00:00:00+08:00 \
        --end 2026-11-12T00:00:00+08:00 [--by-turn]
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path

FIELDS=('input_tokens','cached_input_tokens','output_tokens','total_tokens')
# DSH v4 key -> the canonical key name used by usage_audit.py
KEYMAP={'inputTokens':'input_tokens','cacheReadTokens':'cached_input_tokens',
        'cacheWriteTokens':'cache_write_tokens','outputTokens':'output_tokens',
        'totalTokens':'total_tokens'}


def timestamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Use timestamps with an explicit timezone')
    return result


def audit(lines,start,end,by_turn=False):
    if start>=end:raise ValueError('Start must precede end')
    totals={key:0 for key in FIELDS}|{'cache_write_tokens':0,'uncached_input_tokens':0,'prompt_tokens':0}
    counted=rejected=0
    turns={}
    for line in lines:
        if '"usage"' not in line:continue
        event=json.loads(line)
        if event.get('type')!='assistant/message':continue
        usage=event.get('data',{}).get('usage') or {}
        if not usage:continue
        row={KEYMAP[key]:usage.get(key,0) for key in KEYMAP}
        if any(type(value) is not int or value<0 for value in row.values()):
            rejected+=1;continue
        # A v4 usage block is per-request: prompt plus completion must equal the total.
        if row['input_tokens']+row['cached_input_tokens']+row['output_tokens']!=row['total_tokens']:
            rejected+=1;continue
        moment=datetime.fromtimestamp(event['time']/1000,tz=timezone.utc)
        if not start<=moment<end:continue
        counted+=1
        for key in FIELDS:totals[key]+=row[key]
        totals['cache_write_tokens']+=row['cache_write_tokens']
        totals['uncached_input_tokens']+=row['input_tokens']
        totals['prompt_tokens']+=row['input_tokens']+row['cached_input_tokens']
        if by_turn:
            turn=turns.setdefault(str(event['data'].get('turn')),{key:0 for key in FIELDS})
            for key in FIELDS:turn[key]+=row[key]
    result={'usage_updates':counted,'rejected_records':rejected,**totals}
    if by_turn:result['by_turn']=dict(sorted(turns.items(),key=lambda item:int(item[0]) if item[0].isdigit() else -1))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs',type=Path,nargs='+')
    parser.add_argument('--start',type=timestamp,required=True)
    parser.add_argument('--end',type=timestamp,required=True)
    parser.add_argument('--by-turn',action='store_true')
    args=parser.parse_args()
    logs=[p.resolve(strict=True) for p in args.logs]
    if len(set(logs))!=len(logs):parser.error('Do not count the same log twice')
    grand={key:0 for key in (*FIELDS,'cache_write_tokens','uncached_input_tokens','prompt_tokens','usage_updates','rejected_records')}
    per_log=[]
    for path in logs:
        with path.open(encoding='utf-8') as stream:row=audit(stream,args.start,args.end,args.by_turn)
        per_log.append({'log':path.name,**row})
        for key in grand:grand[key]+=row[key]
    payload={'start':args.start.isoformat(),'end_exclusive':args.end.isoformat(),
             'log_count':len(logs),'schema':'dsh-session-v4',**grand,'money_estimated':False}
    if args.by_turn:payload['per_log']=per_log
    print(json.dumps(payload,ensure_ascii=False))


if __name__=='__main__':main()
