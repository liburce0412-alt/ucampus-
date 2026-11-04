import unittest
from log_observe import metadata


class LogMetadataTests(unittest.TestCase):
    def test_plugin_values_are_never_retained(self):
        events=metadata('D/UCSOEPlugin: StartWithCallback_secret {"engineType":2,"requestId":"private","token":"secret","audioUrl":"hidden"}')
        self.assertEqual(events,[{'kind':'plugin_log','plugin':'UCSOEPlugin','fields':['audioUrl','engineType','requestId'],'action':'start'}])

    def test_route_removes_identity_credentials_query_fragment(self):
        event=metadata('https://user:password@birdflock.unipus.cn/soe-prod/123/uls-c87ec244-6e55-4d49-923e-d10e23474445.mp3?token=secret#private')[0]
        self.assertEqual(event['origin'],'https://birdflock.unipus.cn')
        self.assertEqual(event['path'],'/soe-prod/[id]/[id].mp3')
        self.assertNotIn('secret',str(event))

    def test_unrelated_logs_and_hosts_are_ignored(self):
        self.assertEqual(metadata('private line https://unipus.cn.attacker.example/a?token=secret'),[])
        self.assertEqual(metadata('https://['),[])

    def test_preserves_only_candidate_business_route(self):
        event=metadata('https://u.unipus.cn/api/training/progress?userId=1')[0]
        self.assertEqual(event['path'],'/api/training/progress')
        self.assertIn('not proof',event['scope'])

    def test_short_lowercase_identity_is_redacted_too(self):
        event=metadata('https://u.unipus.cn/api/task/shortsecret/training')[0]
        self.assertEqual(event['path'],'/api/task/[id]/training')
        self.assertNotIn('shortsecret',str(event))


if __name__=='__main__':unittest.main()
