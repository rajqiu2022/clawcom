"""Quality gates for the Agent testing pipeline."""


def _result(passed, reasons=None):
    return {
        'passed': bool(passed),
        'status': 'passed' if passed else 'blocked',
        'reasons': reasons or [],
    }


def evaluate_requirement_review_contract(metrics):
    """Gate for the requirement review post."""
    metrics = metrics or {}
    total = int(metrics.get('total_requirements') or 0)
    reviewed = int(metrics.get('reviewed_requirements') or 0)
    reasons = []
    if total <= 0:
        reasons.append('需求列表为空，不能进入后续工序')
    if reviewed < total:
        reasons.append('需求评审结论不完整：%s/%s' % (reviewed, total))
    return _result(not reasons, reasons)


def evaluate_engineering_analysis_contract(metrics):
    """Gate for engineering impact / implementation-status analysis."""
    metrics = metrics or {}
    total = int(metrics.get('total_requirements') or 0)
    missing = list(metrics.get('requirements_without_status') or [])
    reasons = []
    if total <= 0:
        reasons.append('需求列表为空，不能进行工程分析')
    if missing:
        reasons.append('实现状态未评估：%s' % ', '.join(map(str, missing[:10])))
    return _result(not reasons, reasons)


def evaluate_case_design_contract(metrics):
    """Gate for requirement-to-testcase coverage."""
    metrics = metrics or {}
    gaps = list(metrics.get('gap_requirements') or [])
    reasons = []
    if int(metrics.get('total_requirements') or 0) <= 0:
        reasons.append('需求列表为空，不能设计用例')
    if gaps:
        reasons.append('需求到用例覆盖缺口：%s 个' % len(gaps))
    return _result(not reasons, reasons)


def evaluate_independent_review_contract(metrics):
    """Gate for independent reviewer handoff."""
    metrics = metrics or {}
    reviewer = metrics.get('reviewer_claw_id')
    producers = set(metrics.get('producer_claw_ids') or [])
    reasons = []
    if reviewer in producers:
        reasons.append('独立评审岗不能评审自己的产出')
    if int(metrics.get('review_comments') or 0) <= 0:
        reasons.append('缺少评审意见')
    return _result(not reasons, reasons)


def evaluate_contract(contract_key, metrics):
    mapping = {
        'requirement_review': evaluate_requirement_review_contract,
        'engineering_analysis': evaluate_engineering_analysis_contract,
        'case_design': evaluate_case_design_contract,
        'independent_review': evaluate_independent_review_contract,
    }
    fn = mapping.get(contract_key)
    if not fn:
        return _result(False, ['未知流水线契约：%s' % contract_key])
    return fn(metrics)
