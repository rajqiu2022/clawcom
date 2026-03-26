#!/usr/bin/env python3
"""
MCP (Model Context Protocol) HTTP+SSE 协议支持
OpenClaw 客户端可以通过这个端点连接到 openclaw-manager
"""

import json
from datetime import datetime, date, time
from flask import Blueprint, request, Response, stream_with_context
from app import db
from app.models import OpenClawInstance, DailyReport, hash_token
from functools import wraps

mcp_bp = Blueprint("mcp", __name__, url_prefix="/mcp")


def require_claw_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return json.dumps({"error": "Missing auth token"}), 401
        token = auth[7:]
        claw = OpenClawInstance.query.filter_by(api_token_hash=hash_token(token)).first()
        if not claw:
            return json.dumps({"error": "Invalid token"}), 403
        return f(claw=claw, *args, **kwargs)
    return decorated


@mcp_bp.route("/<int:claw_id>", methods=["POST"])
@require_claw_token
def mcp_handler(claw_id, claw=None):
    """
    MCP JSON-RPC 处理器
    """
    try:
        payload = request.get_json()
        method = payload.get("method")
        params = payload.get("params", {})
        req_id = payload.get("id")

        if method == "tools/list":
            return json.dumps({
                "jsonrpc": "2.0",
                "result": {
                    "tools": [
                        {"name": "manager_heartbeat", "description": "发送心跳到管理后台", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "manager_get_info", "description": "获取OpenClaw信息", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "manager_get_config", "description": "获取配置和Skills", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "manager_submit_report", "description": "提交工作日报", "inputSchema": {"type": "object", "properties": {"tasks_completed": {"type": "string"}, "knowledge_recorded": {"type": "string"}, "experience_shared": {"type": "string"}, "knowledge_learned": {"type": "string"}}}},
                        {"name": "manager_list_reports", "description": "查看日报历史", "inputSchema": {"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}},
                    ]
                },
                "id": req_id
            })

        elif method == "tools/call":
            r = call_manager_tool(claw, params.get("name"), params.get("arguments", {}))
            return json.dumps({"jsonrpc": "2.0", "result": r, "id": req_id})

        return json.dumps({"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown: {method}"}, "id": req_id})

    except Exception as e:
        return json.dumps({"jsonrpc": "2.0", "error": {"message": str(e)}})


def call_manager_tool(claw, name, args):
    """执行 manager 工具"""

    if name == "manager_heartbeat":
        claw.status = "online"
        claw.last_heartbeat = datetime.utcnow()
        db.session.commit()
        return {"content": [{"type": "text", "text": "ok"}]}

    elif name == "manager_get_info":
        return {"content": [{"type": "text", "text": f"ID: {claw.id}, 名称: {claw.name}, 角色: {claw.role}"}]}

    elif name == "manager_get_config":
        skills = "暂无Skills"
        if claw.skills:
            skills = ", ".join([s.skill.name for s in claw.skills if s.enabled])
        return {"content": [{"type": "text", "text": f"配置: {claw.name}, 项目: {claw.project_name or '未分配'}, Skills: {skills}"}]}

    elif name == "manager_submit_report":
        rd = args.get("report_date", date.today().isoformat())
        rt = args.get("report_time", datetime.now().strftime("%H:%M"))
        hour, minute = map(int, rt.split(":"))
        report = DailyReport(
            openclaw_id=claw.id,
            report_date=date.fromisoformat(rd),
            report_time=time(hour, minute),
            tasks_completed=args.get("tasks_completed"),
            knowledge_recorded=args.get("knowledge_recorded"),
            experience_shared=args.get("experience_shared"),
            knowledge_learned=args.get("knowledge_learned"),
        )
        db.session.add(report)
        db.session.commit()
        return {"content": [{"type": "text", "text": f"日报已提交: {rd} {rt}"}]}

    elif name == "manager_list_reports":
        reports = DailyReport.query.filter_by(openclaw_id=claw.id).order_by(
            DailyReport.report_date.desc(), DailyReport.report_time.desc()
        ).limit(10).all()
        if not reports:
            return {"content": [{"type": "text", "text": "暂无日报记录"}]}
        text = "\n".join([f"{r.report_date} {r.report_time}: {r.tasks_completed or '无'}" for r in reports])
        return {"content": [{"type": "text", "text": text}]}

    return {"content": [{"type": "text", "text": f"未知工具: {name}"}]}


@mcp_bp.route("/<int:claw_id>/sse", methods=["GET"])
@require_claw_token
def mcp_sse(claw_id, claw=None):
    """MCP SSE 流式端点"""
    def generate():
        yield f"event: connected\ndata: {json.dumps({'type': 'connected', 'claw_id': claw.id})}\n\n"
    return Response(generate(), mimetype="text/event-stream")
