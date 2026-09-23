"""Plan-scoped supervision, durable events and command receipts."""
from app import db
from app.models import AgentTeam, _now


class PlanSupervisor(db.Model):
    __tablename__ = 'plan_supervisors'
    plan_id = db.Column(db.Integer, db.ForeignKey('test_plans.id'), primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('agent_teams.id'), index=True)
    orchestrator_claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    mission_id = db.Column(db.Integer, db.ForeignKey('workflow_missions.id'), unique=True)
    status = db.Column(db.String(24), nullable=False, default='waiting')
    cursor = db.Column(db.Integer, nullable=False, default=0)
    acknowledged_cursor = db.Column(db.Integer, nullable=False, default=0)
    next_check_at = db.Column(db.DateTime, index=True)
    resume_condition = db.Column(db.String(32), default='timer_or_event')
    last_progress_at = db.Column(db.DateTime)
    last_decision_json = db.Column(db.JSON)
    observations_json = db.Column(db.JSON)
    wake_message_id = db.Column(db.Integer)
    last_wake_at = db.Column(db.DateTime)
    lease_owner = db.Column(db.String(128))
    fencing_token = db.Column(db.Integer, nullable=False, default=0)
    lease_cursor = db.Column(db.Integer)
    lease_expires_at = db.Column(db.DateTime)
    turn_deadline_at = db.Column(db.DateTime)
    expired_turns = db.Column(db.Integer, nullable=False, default=0)
    start_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    def to_dict(self):
        def stamp(value):
            return value.isoformat() + '+08:00' if value else None
        manager = None
        if self.team_id:
            team = db.session.get(AgentTeam, self.team_id)
            if team:
                active = team.has_manager_authority(
                    self.orchestrator_claw_id)
                manager = {
                    'active': active,
                    'epoch': int(team.manager_epoch or 0),
                    'manager_claw_id': (
                        self.orchestrator_claw_id if active else None),
                    'session_id': (
                        'team-manager:%s:%s:%s' % (
                            team.id, int(team.version or 1),
                            self.orchestrator_claw_id)
                        if active else ''),
                    'expires_at': None,
                    'mode': 'team_role_assignment',
                }
        payload = {name: getattr(self, name) for name in (
            'plan_id', 'team_id', 'orchestrator_claw_id', 'mission_id', 'status', 'cursor',
            'acknowledged_cursor', 'resume_condition', 'wake_message_id', 'lease_owner',
            'fencing_token', 'lease_cursor', 'expired_turns')}
        payload.update({
            'work_item_id': 'plan-supervisor:%s' % self.plan_id,
            'next_check_at': stamp(self.next_check_at),
            'last_progress_at': stamp(self.last_progress_at),
            'lease_expires_at': stamp(self.lease_expires_at),
            'turn_deadline_at': stamp(self.turn_deadline_at),
            'last_decision': self.last_decision_json or {},
            'manager_lease': manager,
        })
        return payload


class PlanSupervisorEvent(db.Model):
    __tablename__ = 'plan_supervisor_events'
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('test_plans.id'), nullable=False, index=True)
    event_key = db.Column(db.String(64), nullable=False)
    kind = db.Column(db.String(48), nullable=False)
    payload_json = db.Column(db.JSON, nullable=False)
    # Assigned while holding supervisor row lock, not source event insert order.
    sequence = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    __table_args__ = (
        db.UniqueConstraint('plan_id', 'event_key', name='uq_plan_supervisor_event'),
        db.Index('ix_plan_supervisor_sequence', 'plan_id', 'sequence'),
    )


class PlanSupervisorReceipt(db.Model):
    __tablename__ = 'plan_supervisor_receipts'
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('test_plans.id'), nullable=False)
    action = db.Column(db.String(32), nullable=False)
    command_key = db.Column(db.String(96), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    response_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    __table_args__ = (db.UniqueConstraint('plan_id', 'action', 'command_key',
                                        name='uq_plan_supervisor_receipt'),)
