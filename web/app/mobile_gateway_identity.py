"""Verify identity headers issued by the TAI mobile H5 gateway.

The mobile gateway signs every request after phone iOA authentication.  A
plain STAFFNAME header is never an identity: the application token, timestamp,
request sequence and signature must all match first.
"""
import hashlib
import hmac
import re
import time


_MAX_CLOCK_SKEW = 180
_SIGNATURE = re.compile(r'^[0-9A-Fa-f]{64}$')


def verify_mobile_gateway_headers(headers, token, now=None):
    """Return (state, identity): state is absent, unconfigured, invalid or valid."""
    names = ('TIMESTAMP', 'SIGNATURE', 'STAFFID', 'STAFFNAME', 'X-RIO-SEQ', 'X-EXT-DATA')
    values = {name: (headers.get(name) or '').strip() for name in names}
    # PC NGN also injects SIGNATURE/TIMESTAMP/X-RIO-SEQ.  Only mobile
    # identity-bearing requests enter this verifier.
    if not any(values[name] for name in ('STAFFID', 'STAFFNAME')):
        return 'absent', None
    if not token:
        return 'unconfigured', None
    timestamp = values['TIMESTAMP']
    signature = values['SIGNATURE']
    staff_id = values['STAFFID']
    staff_name = values['STAFFNAME']
    sequence = values['X-RIO-SEQ']
    if (not timestamp.isascii() or not timestamp.isdecimal()
            or not _SIGNATURE.fullmatch(signature)
            or not staff_id.isascii() or not staff_id.isdecimal()
            or not 1 <= len(staff_id) <= 20
            or not sequence or len(sequence) > 128
            or not staff_name or len(staff_name) > 100):
        return 'invalid', None
    current = int(time.time() if now is None else now)
    if abs(current - int(timestamp)) > _MAX_CLOCK_SKEW:
        return 'invalid', None
    # The gateway's documented format includes the comma-separated fields,
    # with an empty X-EXT-DATA for phone iOA requests.
    message = (timestamp + token + sequence + ',' + staff_id + ','
               + staff_name + ',' + values['X-EXT-DATA'] + timestamp)
    expected = hashlib.sha256(message.encode('utf-8')).hexdigest().upper()
    if not hmac.compare_digest(expected, signature.upper()):
        return 'invalid', None
    return 'valid', {'staff_id': staff_id, 'staff_name': staff_name}
