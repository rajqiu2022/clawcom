"""种子数据：初始化标准 Skills、Rules 和默认项目"""
from app import db
from app.models import Skill, Rule, Project


STANDARD_SKILLS = [
    {
        'name': 'test-report-generator',
        'display_name': '测试报告生成器',
        'description': '根据测试数据和 Bug 记录，自动生成结构化的测试报告。支持迭代报告、专项报告、发布评审报告等多种格式。',
        'category': 'standard',
        'trigger_phrase': '生成测试报告',
        'scope': 'global',
        'template_content': '''# 测试报告生成器

## 触发方式
用户说"生成测试报告"、"出一份测试报告"等

## 工作流程
1. 询问报告类型（迭代报告/专项报告/发布评审）
2. 收集测试数据（从 Memos 和本地记录）
3. 统计 Bug 分布（按严重程度、模块、状态）
4. 生成风险评估
5. 输出格式化报告

## 报告结构
- 测试概述（时间、范围、资源）
- 测试执行情况（用例数、通过率）
- 缺陷分析（新增、修复、遗留）
- 风险评估（高/中/低风险项）
- 质量结论与建议
''',
    },
    {
        'name': 'bug-weekly-summary',
        'display_name': 'Bug 周报生成器',
        'description': '自动汇总一周内的 Bug 数据，生成周报。包含 Bug 趋势、热点模块、修复率等关键指标。',
        'category': 'standard',
        'trigger_phrase': '生成 Bug 周报',
        'scope': 'global',
        'template_content': '''# Bug 周报生成器

## 触发方式
用户说"生成 Bug 周报"、"出一份 Bug 周报"等

## 工作流程
1. 从知识库拉取本周 #bug-pattern 标签的 memo
2. 统计新增/修复/遗留 Bug 数量
3. 分析热点模块和高频问题
4. 与上周数据对比，计算趋势
5. 输出周报

## 周报结构
- 本周概览（新增/修复/遗留）
- Bug 趋势图（近4周对比）
- 热点模块 TOP5
- 严重 Bug 跟踪
- 下周关注点
''',
    },
    {
        'name': 'release-checklist',
        'display_name': '发版检查清单',
        'description': '提供版本发布前的系统性检查清单。确保功能验证、性能基线、兼容性、安全性等各维度都已覆盖。',
        'category': 'standard',
        'trigger_phrase': '发版检查',
        'scope': 'global',
        'template_content': '''# 发版检查清单

## 触发方式
用户说"发版检查"、"版本发布前检查"等

## 检查维度
### P0 - 必须通过
- [ ] 核心功能回归测试全部通过
- [ ] 无 P0/P1 级 Bug 遗留
- [ ] 性能基线测试达标
- [ ] 登录/支付等关键路径正常

### P1 - 应该通过
- [ ] 兼容性测试覆盖主流机型
- [ ] 网络异常场景测试
- [ ] 数据迁移验证（如有）
- [ ] 灰度方案确认

### P2 - 建议检查
- [ ] 日志和监控配置
- [ ] 回滚方案就绪
- [ ] 发布公告准备
- [ ] 值班安排确认
''',
    },
    {
        'name': 'performance-analyzer',
        'display_name': '性能分析助手',
        'description': '分析性能测试数据，对比基线指标，识别性能瓶颈和退化点。支持帧率、内存、CPU、启动时间等多维度分析。',
        'category': 'standard',
        'trigger_phrase': '分析性能数据',
        'scope': 'global',
        'template_content': '''# 性能分析助手

## 触发方式
用户说"分析性能数据"、"性能分析"等

## 分析维度
- **帧率**: 平均帧率、1% Low、卡顿率
- **内存**: 峰值、平均值、泄漏检测
- **CPU**: 平均占用、峰值、大核占比
- **启动时间**: 冷启动、热启动
- **耗电量**: mA/h、温度曲线

## 分析流程
1. 收集本次测试数据
2. 对比历史基线数据（从知识库 #performance-case）
3. 标注退化项（超过阈值的指标）
4. 给出优化建议
5. 输出分析报告

## 基线标准
- 低端机: >= 25fps, 内存 < 800MB
- 中端机: >= 30fps, 内存 < 1.2GB
- 高端机: >= 60fps, 内存 < 1.5GB
''',
    },
    {
        'name': 'test-case-reviewer',
        'display_name': '测试用例评审',
        'description': '对测试用例进行质量评审，检查覆盖度、边界条件、异常场景等。基于知识库中的 Bug 模式提出补充建议。',
        'category': 'standard',
        'trigger_phrase': '评审测试用例',
        'scope': 'global',
        'template_content': '''# 测试用例评审

## 触发方式
用户说"评审测试用例"、"帮我看看用例"等

## 评审维度
### 覆盖度检查
- 正常流程是否覆盖
- 异常流程是否覆盖
- 边界值是否覆盖
- 并发/竞态场景

### 基于知识库的补充
- 查询 #bug-pattern 中相关模块的历史 Bug
- 根据 Bug 模式补充可能遗漏的用例
- 参考 #test-checklist 中的检查项

### 质量评估
- 用例描述是否清晰
- 预期结果是否明确
- 前置条件是否完整
- 优先级标注是否合理

## 输出
- 评审意见（通过/需修改）
- 建议补充的用例列表
- 参考的知识库条目
''',
    },
]


# 注册 Skill - 用于 OpenClaw 注册到 Hub 时执行
REGISTRATION_SKILL = {
    'name': 'registration-skill',
    'display_name': '注册技能（标准化）',
    'description': '标准化注册流程：注册到 Hub、安装标准化 Rules 和 Skills。本 Skill 由 Hub 管理员配置，新 OpenClaw 注册时自动执行。',
    'category': 'standard',
    'is_standard': True,
    'trigger_phrase': '注册 Hub',
    'scope': 'global',
    'template_content': '''# 注册技能

## 功能说明
本 Skill 用于将 OpenClaw 注册到 Hub 系统，并自动安装标准化的 Rules 和 Skills。

## 执行流程
1. 调用 Hub 注册接口，提交 OpenClaw 基本信息
2. 获取分配的 OpenClaw ID 和 API Token
3. 安装标准化的 Rules
4. 安装标准化的 Skills
5. 配置工作规范

## 待填写内容
- Hub 注册接口地址
- 注册参数模板
- 标准化 Rules 列表
- 标准化 Skills 列表
''',
}


# 标准 Rules 模板
STANDARD_RULES = [
    {
        'name': 'base-workflow',
        'display_name': '基础工作规范',
        'description': '所有 OpenClaw 必须遵守的基础工作规范，包括任务执行、日报提交、异常处理等基本要求。',
        'category': 'standard',
        'scope': 'global',
        'is_standard': True,
        'content_template': '''# 基础工作规范

## 任务执行
1. 收到任务后，先确认理解任务目标
2. 遇到不清晰的地方，先提问再执行
3. 任务完成后，简要汇报结果

## 日报提交
1. 每日按计划时间提交日报
2. 日报内容包含：完成事项、学习收获、问题记录
3. 如有紧急任务，提前报备

## 异常处理
1. 发现异常情况及时上报
2. 遇到阻塞问题，主动寻求协助
3. 重大问题不擅自决定，汇报后执行
''',
    },
    {
        'name': 'security-baseline',
        'display_name': '安全基线规范',
        'description': '安全相关的基本规范，确保 OpenClaw 操作符合安全要求。',
        'category': 'standard',
        'scope': 'global',
        'is_standard': True,
        'content_template': '''# 安全基线规范

## 权限管理
1. 不尝试越权操作
2. 不获取超出职责范围的系统权限
3. 敏感操作需确认授权

## 数据处理
1. 不操作真实生产数据
2. 测试数据需脱敏处理
3. 敏感信息不外泄

## 操作规范
1. 危险命令需二次确认
2. 删除操作需谨慎
3. 不执行来源不明的代码
''',
    },
]


def seed_skills():
    """初始化标准 Skills（跳过已存在的）"""
    created = 0
    for skill_data in STANDARD_SKILLS:
        existing = Skill.query.filter_by(name=skill_data['name']).first()
        if not existing:
            skill = Skill(**skill_data)
            db.session.add(skill)
            created += 1

    # 初始化注册 Skill
    if not Skill.query.filter_by(name=REGISTRATION_SKILL['name']).first():
        skill = Skill(**REGISTRATION_SKILL)
        db.session.add(skill)
        created += 1

    db.session.commit()
    return created


def seed_rules():
    """初始化标准 Rules（跳过已存在的）"""
    created = 0
    for rule_data in STANDARD_RULES:
        existing = Rule.query.filter_by(name=rule_data['name']).first()
        if not existing:
            rule = Rule(**rule_data)
            db.session.add(rule)
            created += 1

    db.session.commit()
    return created


STANDARD_PROJECTS = [
    {'name': 'QQ飞车', 'tapd_workspace_id': '1000047'},
    {'name': 'QQ飞车手游版', 'tapd_workspace_id': '10124081'},
    {'name': '合金弹头', 'tapd_workspace_id': '20375982'},
    {'name': '魂斗罗', 'tapd_workspace_id': '10102081'},
    {'name': 'PRacing', 'tapd_workspace_id': '20417582'},
]


def seed_projects():
    """初始化默认项目（跳过已存在的）"""
    created = 0
    for proj_data in STANDARD_PROJECTS:
        existing = Project.query.filter_by(name=proj_data['name']).first()
        if not existing:
            project = Project(**proj_data)
            db.session.add(project)
            created += 1

    db.session.commit()
    return created


def seed_all():
    """执行所有种子数据初始化"""
    skills_count = seed_skills()
    rules_count = seed_rules()
    projects_count = seed_projects()
    return f'创建了 {skills_count} 个标准 Skills，{rules_count} 个标准 Rules，{projects_count} 个默认项目'
