import hashlib
import importlib.util
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1] / 'web/app/mobile_gateway_identity.py'
spec = importlib.util.spec_from_file_location('mobile_gateway_identity', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
verify_mobile_gateway_headers = module.verify_mobile_gateway_headers


def signed_headers(token='mobile-secret', timestamp='1700000000', name='tester'):
    fields = {'TIMESTAMP': timestamp, 'STAFFID': '12345', 'STAFFNAME': name,
              'X-RIO-SEQ': 'req-1', 'X-EXT-DATA': ''}
    message = (timestamp + token + 'req-1,12345,' + name + ',' + timestamp)
    fields['SIGNATURE'] = hashlib.sha256(message.encode()).hexdigest().upper()
    return fields


class MobileGatewayIdentityTest(unittest.TestCase):
    def test_accepts_signed_phone_ioa_identity(self):
        state, identity = verify_mobile_gateway_headers(
            signed_headers(), 'mobile-secret', now=1700000001)
        self.assertEqual(state, 'valid')
        self.assertEqual(identity, {'staff_id': '12345', 'staff_name': 'tester'})

    def test_rejects_spoofed_and_expired_headers(self):
        headers = signed_headers()
        headers['STAFFNAME'] = 'admin'
        self.assertEqual(verify_mobile_gateway_headers(
            headers, 'mobile-secret', now=1700000001)[0], 'invalid')
        self.assertEqual(verify_mobile_gateway_headers(
            signed_headers(), 'mobile-secret', now=1700000181)[0], 'invalid')

    def test_requires_configured_token_and_identity(self):
        self.assertEqual(verify_mobile_gateway_headers({}, 'mobile-secret')[0],
                         'absent')
        self.assertEqual(verify_mobile_gateway_headers(
            {'SIGNATURE': 'A' * 64, 'TIMESTAMP': '1700000000',
             'X-RIO-SEQ': 'pc-ngn'}, 'mobile-secret', now=1700000000)[0],
            'absent')
        self.assertEqual(verify_mobile_gateway_headers(
            signed_headers(), '', now=1700000000)[0], 'unconfigured')


if __name__ == '__main__':
    unittest.main()
