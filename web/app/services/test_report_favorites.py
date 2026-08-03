def favorite_owner_from_caller(caller):
    """把当前调用方规范化成测试报告收藏归属。

    收藏是个人/Agent 维度：Web 用户归 user_id，Bearer Agent 归 claw_id。
    """
    if not caller:
        raise ValueError('未认证')
    if caller.get('type') == 'user' and caller.get('user_id'):
        return {'user_id': int(caller['user_id']), 'claw_id': None}
    if caller.get('type') == 'openclaw' and caller.get('claw_id'):
        return {'user_id': None, 'claw_id': int(caller['claw_id'])}
    raise ValueError('无法识别收藏归属')
