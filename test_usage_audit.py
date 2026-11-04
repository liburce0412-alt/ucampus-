import json
import unittest
from usage_audit import audit,timestamp


def event(hour,totals,last):
    keys=('input_tokens','cached_input_tokens','output_tokens','total_tokens')
    return json.dumps({'type':'event_msg','timestamp':f'2026-11-04T{hour:02}:00:00Z',
                       'payload':{'type':'token_count','info':{'total_token_usage':dict(zip(keys,totals)),
                                                            'last_token_usage':dict(zip(keys,last))}}})


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


if __name__=='__main__':unittest.main()
