import json
import unittest
from usage_audit_dsh import audit,timestamp


def event(hour,* ,turn=1,usage=None,total=None):
    usage=usage or {'inputTokens':100,'cacheReadTokens':800,'cacheWriteTokens':0,'outputTokens':10,'totalTokens':910}
    if total is not None:usage={**usage,'totalTokens':total}
    return json.dumps({'type':'assistant/message','time':int(timestamp(f'2026-11-04T{hour:02}:00:00Z').timestamp()*1000),
                       'data':{'turn':turn,'usage':usage}})


class UsageDshTests(unittest.TestCase):
    def test_counts_v4_usage_inside_the_window_only(self):
        lines=[event(1),event(2),event(3)]
        result=audit(lines,timestamp('2026-11-04T01:00:00Z'),timestamp('2026-11-04T03:00:00Z'))
        self.assertEqual(result['usage_updates'],2)
        self.assertEqual(result['uncached_input_tokens'],200)
        self.assertEqual(result['cached_input_tokens'],1600)
        self.assertEqual(result['output_tokens'],20)
        self.assertEqual(result['total_tokens'],1820)

    def test_inconsistent_total_is_rejected_not_estimated(self):
        lines=[event(1,total=9999)]
        result=audit(lines,timestamp('2026-11-04T00:00:00Z'),timestamp('2026-11-05T00:00:00Z'))
        self.assertEqual(result['usage_updates'],0)
        self.assertEqual(result['rejected_records'],1)
        self.assertEqual(result['total_tokens'],0)

    def test_calls_before_each_update_are_not_reported(self):
        lines=['{"type":"tool/call","time":0,"data":{}}','{"type":"assistant/message","time":0,"data":{"turn":1}}']
        result=audit(lines,timestamp('2026-11-04T00:00:00Z'),timestamp('2026-11-05T00:00:00Z'))
        self.assertEqual(result['usage_updates'],0)

    def test_grouping_by_turn_sums_per_request_counters(self):
        lines=[event(1,turn=1),event(2,turn=1),event(3,turn=2)]
        result=audit(lines,timestamp('2026-11-04T00:00:00Z'),timestamp('2026-11-05T00:00:00Z'),by_turn=True)
        self.assertEqual(result['by_turn']['1']['total_tokens'],1820)
        self.assertEqual(result['by_turn']['2']['total_tokens'],910)

    def test_no_unscoped_timezone_and_end_is_exclusive(self):
        with self.assertRaises(ValueError):timestamp('2026-11-04T02:00:00')
        result=audit([event(2)],timestamp('2026-11-04T01:00:00Z'),timestamp('2026-11-04T02:00:00Z'))
        self.assertEqual(result['usage_updates'],0)


if __name__=='__main__':unittest.main()
