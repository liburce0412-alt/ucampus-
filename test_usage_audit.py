import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from usage_audit import audit,timestamp,main


def event(hour,totals,last):
    keys=('input_tokens','cached_input_tokens','output_tokens','total_tokens')
    return json.dumps({'type':'event_msg','timestamp':f'2026-11-04T{hour:02}:00:00Z',
                       'payload':{'type':'token_count','info':{'total_token_usage':dict(zip(keys,totals)),
                                                            'last_token_usage':dict(zip(keys,last))}}})


def dsh_event(hour,usage=None):
    if usage is None:
        usage={'inputTokens':100,'cacheReadTokens':800,'cacheWriteTokens':0,'outputTokens':10,'totalTokens':910}
    return json.dumps({'type':'assistant/message','time':int(timestamp(f'2026-11-04T{hour:02}:00:00Z').timestamp()*1000),
                       'data':{'turn':1,'usage':usage,'content':'PRIVATE_TRANSCRIPT'}})


class UsageTests(unittest.TestCase):
    def test_duplicates_window_cache_and_previous_history(self):
        first=event(1,(1000,800,100,1100),(100,80,10,110))
        second=event(2,(1200,980,120,1320),(200,180,20,220))
        result=audit([first,first,second,second],timestamp('2026-11-04T02:00:00Z'),timestamp('2026-11-04T03:00:00Z'))
        self.assertEqual(result['usage_updates'],1)
        self.assertEqual(result['uncached_input_tokens'],20)
        self.assertEqual(result['cached_input_tokens'],180)
        self.assertEqual(result['output_tokens'],20)

    def test_counter_reset_or_missing_events_is_not_silently_estimated(self):
        first=event(1,(1000,800,100,1100),(100,80,10,110))
        for following in (event(2,(200,180,20,220),(200,180,20,220)),event(2,(1400,1000,140,1540),(200,100,20,220))):
            with self.assertRaises(ValueError):audit([first,following],timestamp('2026-11-04T00:00:00Z'),timestamp('2026-11-05T00:00:00Z'))

    def test_no_unscoped_timezone_and_end_is_exclusive(self):
        with self.assertRaises(ValueError):timestamp('2026-11-04T02:00:00')
        result=audit([event(2,(100,80,10,110),(100,80,10,110))],timestamp('2026-11-04T01:00:00Z'),timestamp('2026-11-04T02:00:00Z'))
        self.assertEqual(result['usage_updates'],0)

    def test_both_formats_report_the_same_input_and_output_meanings(self):
        start=timestamp('2026-11-04T00:00:00Z');end=timestamp('2026-11-05T00:00:00Z')
        counts=(900,800,10,910)
        codex=audit([event(2,counts,counts)],start,end)
        dsh=audit([dsh_event(2)],start,end)
        self.assertEqual(codex.pop('schema'),'codex');self.assertEqual(dsh.pop('schema'),'dsh-v4')
        self.assertEqual(codex,dsh)
        self.assertEqual(dsh['input_tokens'],900);self.assertEqual(dsh['uncached_input_tokens'],100)

    def test_dsh_equal_per_request_usage_is_not_a_duplicate_or_reset(self):
        lines=[dsh_event(1),dsh_event(2),dsh_event(3)]
        result=audit(lines,timestamp('2026-11-04T09:00:00+08:00'),timestamp('2026-11-04T11:00:00+08:00'))
        self.assertEqual(result['usage_updates'],2);self.assertEqual(result['input_tokens'],1800)
        self.assertEqual(result['cached_input_tokens'],1600);self.assertEqual(result['output_tokens'],20)
        self.assertEqual(result['total_tokens'],1820)

    def test_dsh_missing_invalid_or_unsupported_counters_fail_without_partial_totals(self):
        start=timestamp('2026-11-04T00:00:00Z');end=timestamp('2026-11-05T00:00:00Z')
        good=json.loads(dsh_event(2))['data']['usage']
        missing={key:value for key,value in good.items() if key!='cacheReadTokens'}
        for bad in ({},missing,{**good,'inputTokens':True},{**good,'outputTokens':-1},
                    {**good,'totalTokens':999},{**good,'cacheWriteTokens':1}):
            with self.subTest(usage=bad):
                with self.assertRaises(ValueError):audit([dsh_event(1),dsh_event(2,bad)],start,end)

    def test_dsh_counters_outside_window_do_not_contaminate_the_stage(self):
        result=audit([dsh_event(1,{}),dsh_event(2)],timestamp('2026-11-04T02:00:00Z'),timestamp('2026-11-04T03:00:00Z'))
        self.assertEqual(result['usage_updates'],1);self.assertEqual(result['total_tokens'],910)
        invalid=json.loads(dsh_event(2));invalid['time']=True
        with self.assertRaisesRegex(ValueError,'timestamp'):
            audit([json.dumps(invalid)],timestamp('2026-11-04T00:00:00Z'),timestamp('2026-11-05T00:00:00Z'))

    def test_mixed_schema_and_missing_usage_are_not_silent_zero_reports(self):
        start=timestamp('2026-11-04T00:00:00Z');end=timestamp('2026-11-05T00:00:00Z')
        with self.assertRaisesRegex(ValueError,'mix'):
            audit([event(2,(900,800,10,910),(900,800,10,910)),dsh_event(2)],start,end)
        for lines in ([],['{"type":"tool/call"}'],['{"type":"assistant/message","data":{}}']):
            with self.assertRaisesRegex(ValueError,'No supported'):audit(lines,start,end)
        with self.assertRaisesRegex(ValueError,'timezone'):audit([dsh_event(2)],start.replace(tzinfo=None),end)

    def test_codex_missing_and_inconsistent_counters_are_rejected(self):
        start=timestamp('2026-11-04T00:00:00Z');end=timestamp('2026-11-05T00:00:00Z')
        missing=json.loads(event(2,(900,800,10,910),(900,800,10,910)))
        del missing['payload']['info']['last_token_usage']['cached_input_tokens']
        for line in (json.dumps(missing),event(2,(900,800,10,999),(900,800,10,999)),
                     event(2,(100,800,10,110),(100,800,10,110))):
            with self.assertRaises(ValueError):audit([line],start,end)

    def test_cli_normalizes_separate_formats_and_rejects_a_repeated_path(self):
        with tempfile.TemporaryDirectory() as directory:
            first=Path(directory)/'private-codex.jsonl';second=Path(directory)/'private-dsh.jsonl'
            first.write_text(event(2,(900,800,10,910),(900,800,10,910)),encoding='utf-8')
            second.write_text(dsh_event(2),encoding='utf-8')
            args=['usage_audit.py',str(first),str(second),'--start','2026-11-04T00:00:00Z','--end','2026-11-05T00:00:00Z']
            with patch('sys.argv',args),patch('sys.stdout',new_callable=io.StringIO) as output:main()
            result=json.loads(output.getvalue())
            self.assertEqual(result['input_tokens'],1800);self.assertEqual(result['uncached_input_tokens'],200)
            self.assertEqual(result['log_schemas'],['codex','dsh-v4']);self.assertFalse(result['money_estimated'])
            self.assertNotIn('PRIVATE_TRANSCRIPT',output.getvalue());self.assertNotIn('private-',output.getvalue())
            args[2]=str(first)
            with patch('sys.argv',args),patch('sys.stderr',new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:main()
            self.assertEqual(error.exception.code,2)


if __name__=='__main__':unittest.main()
