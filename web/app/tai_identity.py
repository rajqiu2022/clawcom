"""太湖统一身份认证 —— x-tai-identity 解密。

接入说明：
    `clawteam.woa.com` 已在太湖平台接入（站点 ID: b218faa5-5984-4598-ae33-
    90578d1a1498，归属应用 claw_team，PC 站点）。太湖网关 NGN 在 SLB 层完成
    OA 统一登录后，会把用户身份以 `x-tai-identity` 请求头注入到上游 Flask：

        x-tai-identity: <JWE Compact Serialization>

    本模块负责解密该 header，提取 LoginName / StaffId / Expiration，
    上层用它替代「自己跳 passport 解 ticket」的旧 SSO 流程。

算法：
    * JWE: alg=dir, enc=A256GCM
    * 对称密钥 = 应用 Token（32 字节 UTF-8 字符串）
    * 过期容忍：3 分钟时钟偏差

依赖：
    pip install jwcrypto python-dateutil pytz
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 应用 Token —— 从 https://tai.it.woa.com/apps/claw_team/settings 复制。
# 32 字节 UTF-8 字符串，用作 A256GCM 对称密钥。
TAI_APP_TOKEN = os.getenv('TAI_APP_TOKEN', '')

# 时钟偏差容忍（按子技能 references 建议为 3 分钟）
TAI_CLOCK_SKEW_SECONDS = 180


class Identity:
    """太湖 x-tai-identity 解密后的用户身份。"""

    __slots__ = ('login_name', 'staff_id', 'expiration', 'raw')

    def __init__(self, login_name: str, staff_id: int,
                 expiration: str, raw: dict):
        self.login_name = login_name
        self.staff_id = staff_id
        self.expiration = expiration
        self.raw = raw

    def to_dict(self) -> dict:
        return {
            'login_name': self.login_name,
            'staff_id': self.staff_id,
            'expiration': self.expiration,
        }

    def __repr__(self) -> str:
        return (f'Identity(login_name={self.login_name!r}, '
                f'staff_id={self.staff_id}, expiration={self.expiration!r})')


def decode_x_tai_identity(
    header_value: str,
    token: Optional[str] = None,
) -> Tuple[bool, object]:
    """解密 x-tai-identity 头。

    Args:
        header_value: `x-tai-identity` HTTP 请求头原值（JWE Compact）。
        token: 应用 Token；默认读取 `TAI_APP_TOKEN` 环境变量。

    Returns:
        (ok, payload)
            ok=True  → payload 是 `Identity` 实例
            ok=False → payload 是 dict {error: str}
    """
    if not header_value:
        return False, {'error': 'x-tai-identity 为空'}

    token = token or TAI_APP_TOKEN
    if not token:
        return False, {'error': 'TAI_APP_TOKEN 未配置'}

    key_bytes = token.encode('utf-8')
    if len(key_bytes) != 32:
        return False, {
            'error': (f'应用 Token 长度必须为 32 字节，当前 {len(key_bytes)} '
                      f'字节；请检查 TAI_APP_TOKEN 配置')
        }

    # 延迟导入：避免单元测试/本地启动时强依赖
    try:
        from jwcrypto import jwe, jwk
    except ImportError as e:
        return False, {'error': f'缺少 jwcrypto 依赖（pip install jwcrypto）: {e}'}

    try:
        key = jwk.JWK(kty='oct', k=_b64url(key_bytes))
        token_obj = jwe.JWE()
        token_obj.deserialize(header_value, key=key)
        payload_bytes = token_obj.payload
        payload = json.loads(payload_bytes.decode('utf-8'))
    except Exception as e:
        return False, {'error': f'JWE 解密失败: {e.__class__.__name__}: {e}'}

    login_name = (payload.get('LoginName') or payload.get('loginName') or '').strip()
    staff_id = payload.get('StaffId') or payload.get('staffId') or 0
    expiration = (payload.get('Expiration') or payload.get('expiration') or '').strip()

    if not login_name:
        return False, {'error': 'payload 中缺少 LoginName', 'raw': payload}

    if expiration:
        try:
            # ISO 8601: 2053-04-05T01:50:52.736Z
            exp_dt = _parse_iso8601(expiration)
            now = datetime.datetime.now(datetime.timezone.utc)
            # 给 3 分钟时钟偏差容忍
            if exp_dt + datetime.timedelta(
                seconds=TAI_CLOCK_SKEW_SECONDS
            ) < now:
                return False, {
                    'error': f'x-tai-identity 已过期 (exp={expiration})',
                    'raw': payload,
                }
        except Exception as e:
            logger.warning('[TAI] Expiration 解析失败 %s: %s', expiration, e)

    try:
        staff_id = int(staff_id)
    except Exception:
        staff_id = 0

    return True, Identity(
        login_name=login_name,
        staff_id=staff_id,
        expiration=expiration,
        raw=payload,
    )


def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def _parse_iso8601(s: str) -> datetime.datetime:
    """解析太湖 Expiration 字符串为 aware datetime（UTC）。

    形如：``2053-04-05T01:50:52.736Z`` 或 ``2026-05-12T13:00:00+08:00``。
    Python 3.7+ 的 ``datetime.fromisoformat`` 不认 ``Z``，手动替换为 ``+00:00``。
    """
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt
