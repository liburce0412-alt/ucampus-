import struct
import unittest
from bridge.make_rollback import bump_manifest


def fixture(resource=0x0101021b):
    resources=struct.pack('<HHII',0x180,8,12,resource)
    node=struct.pack('<HHIII',0x102,16,56,1,0xffffffff)
    node+=struct.pack('<IIHHHHHH',0xffffffff,0,20,20,1,0,0,0)
    node+=struct.pack('<IIIHBBI',0xffffffff,0,0xffffffff,8,0,0x10,3)
    return struct.pack('<HHI',3,8,8+len(resources)+len(node))+resources+node


class RollbackTests(unittest.TestCase):
    def test_only_version_value_changes(self):
        source=fixture();result=bump_manifest(source)
        self.assertEqual(result[:-4],source[:-4]);self.assertEqual(struct.unpack('<I',result[-4:])[0],7)

    def test_wrong_version_or_attribute_is_rejected(self):
        for source,expected in [(fixture(),4),(fixture(0x0101021c),3)]:
            with self.assertRaises(ValueError):bump_manifest(source,expected)
        with self.assertRaises(ValueError):bump_manifest(fixture(),3,2)

    def test_truncation_is_rejected(self):
        with self.assertRaises(ValueError):bump_manifest(fixture()[:-1])


if __name__=='__main__':unittest.main()
