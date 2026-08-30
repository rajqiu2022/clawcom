"""Project-scoped entity lineage batch-upsert, query and graph traversal (P0-F)."""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    is_admin_user,
    user_project_ids,
)
from app.models import EntityRelation, Project
from app.services.entity_relations import (
    normalize_entity_id,
    normalize_entity_type,
    normalize_relation_type,
    upsert_entity_relations,
)


_CST = timezone(timedelta(hours=8))


def _request_id():
    return str(request.headers.get('X-Request-ID') or uuid4().hex).strip()[:128]


def _error(code, message, status=400, details=None):
    return jsonify({
        'error': message,
        'code': code,
        'message': message,
        'details': details or {},
        'request_id': _request_id(),
    }), status


def _actor():
    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}', 'claw': claw,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': getattr(user, 'username', '') or f'user:{user.id}',
            'user': user,
        }
    return None


def _can_access_project(actor, project_id):
    if is_admin_user():
        return True
    if actor['type'] == 'claw':
        claw = actor['claw']
        return claw.project_id is None or int(claw.project_id) == int(project_id)
    return int(project_id) in user_project_ids(actor['user'])


def _project_id_or_error(actor, data=None):
    raw = (data or {}).get('project_id') if data is not None else request.args.get(
        'project_id')
    try:
        project_id = int(raw)
    except (TypeError, ValueError):
        return None, _error('INVALID_PROJECT_ID', 'project_id must be an integer')
    if not db.session.get(Project, project_id) or not _can_access_project(
            actor, project_id):
        return None, _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    return project_id, None


def _parse_updated_after(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('updated_after must be an ISO-8601 datetime') from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_CST).replace(tzinfo=None)
    return parsed


@api_bp.route('/entity-relations:batch-upsert', methods=['POST'])
def batch_upsert_entity_relations():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    data = request.get_json(silent=True) or {}
    project_id, project_error = _project_id_or_error(actor, data)
    if project_error:
        return project_error
    relations = data.get('relations')
    if not isinstance(relations, list) or not relations:
        return _error(
            'ENTITY_RELATION_VALIDATION_FAILED',
            'relations must be a non-empty array')
    if len(relations) > 500:
        return _error(
            'ENTITY_RELATION_VALIDATION_FAILED',
            'relations must not contain more than 500 items')
    try:
        result = upsert_entity_relations(project_id, relations, actor['name'])
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error('ENTITY_RELATION_VALIDATION_FAILED', str(exc))
    except IntegrityError:
        # A concurrent natural-key writer won. Re-run after rollback so this
        # request remains an idempotent success instead of exposing a 500.
        db.session.rollback()
        try:
            result = upsert_entity_relations(project_id, relations, actor['name'])
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return _error(
                'ENTITY_RELATION_CONCURRENT_WRITE',
                'Relations changed concurrently; retry the same batch', 409)
    items = sorted(result['items'], key=lambda row: row.id or 0)
    return jsonify({
        'items': [row.to_dict() for row in items],
        'created_count': len(result['created']),
        'updated_count': len(result['updated']),
        'unchanged_count': len(result['unchanged']),
        'total': len(items),
    }), 201 if result['created'] else 200


@api_bp.route('/entity-relations', methods=['GET'])
def list_entity_relations():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    project_id, project_error = _project_id_or_error(actor)
    if project_error:
        return project_error
    query = EntityRelation.query.filter_by(project_id=project_id)
    filters_used = False
    try:
        for prefix in ('from', 'to'):
            entity_type = str(request.args.get(f'{prefix}_type') or '').strip()
            entity_id = str(request.args.get(f'{prefix}_id') or '').strip()
            if bool(entity_type) != bool(entity_id):
                raise ValueError(
                    f'{prefix}_type and {prefix}_id must be provided together')
            if entity_type:
                entity_type = normalize_entity_type(
                    entity_type, f'{prefix}_type')
                entity_id = normalize_entity_id(entity_id, f'{prefix}_id')
                query = query.filter(
                    getattr(EntityRelation, f'{prefix}_type') == entity_type,
                    getattr(EntityRelation, f'{prefix}_id') == entity_id,
                )
                filters_used = True
        relation_type = str(request.args.get('relation_type') or '').strip()
        if relation_type:
            query = query.filter_by(
                relation_type=normalize_relation_type(relation_type))
            filters_used = True
        updated_after = str(request.args.get('updated_after') or '').strip()
        if updated_after:
            query = query.filter(
                EntityRelation.updated_at > _parse_updated_after(updated_after))
            filters_used = True
        page = max(1, int(request.args.get('page', 1)))
        page_size = min(200, max(1, int(
            request.args.get('page_size') or request.args.get('per_page') or 50)))
    except (TypeError, ValueError) as exc:
        return _error('ENTITY_RELATION_QUERY_INVALID', str(exc))
    if not filters_used:
        return _error(
            'ENTITY_RELATION_FILTER_REQUIRED',
            'Provide a from/to entity, relation_type, or updated_after filter')
    total = query.count()
    rows = (query.order_by(EntityRelation.updated_at.asc(), EntityRelation.id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return jsonify({
        'items': [row.to_dict() for row in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': (total + page_size - 1) // page_size,
    })


def _frontier_filter(column_type, column_id, frontier):
    conditions = [
        db.and_(column_type == entity_type, column_id == entity_id)
        for entity_type, entity_id in frontier
    ]
    return db.or_(*conditions)


@api_bp.route('/entity-relations/trace', methods=['GET'])
def trace_entity_relations():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    project_id, project_error = _project_id_or_error(actor)
    if project_error:
        return project_error
    try:
        root_type = normalize_entity_type(
            request.args.get('entity_type'), 'entity_type')
        root_id = normalize_entity_id(request.args.get('entity_id'), 'entity_id')
        direction = str(request.args.get('direction') or 'both').strip().lower()
        if direction not in {'upstream', 'downstream', 'both'}:
            raise ValueError('direction must be upstream, downstream, or both')
        max_depth = int(request.args.get('max_depth', 5))
        max_nodes = int(request.args.get('max_nodes', 300))
        if max_depth < 1 or max_depth > 5:
            raise ValueError('max_depth must be between 1 and 5')
        if max_nodes < 2 or max_nodes > 500:
            raise ValueError('max_nodes must be between 2 and 500')
    except (TypeError, ValueError) as exc:
        return _error('ENTITY_TRACE_QUERY_INVALID', str(exc))

    root = (root_type, root_id)
    node_depths = {root: 0}
    frontier = {root}
    edges = {}
    truncated = False
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        queries = []
        if direction in {'downstream', 'both'}:
            queries.append(EntityRelation.query.filter(
                EntityRelation.project_id == project_id,
                _frontier_filter(
                    EntityRelation.from_type, EntityRelation.from_id, frontier)))
        if direction in {'upstream', 'both'}:
            queries.append(EntityRelation.query.filter(
                EntityRelation.project_id == project_id,
                _frontier_filter(
                    EntityRelation.to_type, EntityRelation.to_id, frontier)))
        found = []
        for query in queries:
            found.extend(query.order_by(EntityRelation.id.asc()).limit(max_nodes).all())
        next_frontier = set()
        for edge in found:
            edges[edge.id] = edge
            for node in (
                (edge.from_type, edge.from_id),
                (edge.to_type, edge.to_id),
            ):
                if node not in node_depths:
                    if len(node_depths) >= max_nodes:
                        truncated = True
                        continue
                    node_depths[node] = depth
                    next_frontier.add(node)
        frontier = next_frontier
        if truncated:
            break
    nodes = [
        {'entity_type': node[0], 'entity_id': node[1], 'depth': depth}
        for node, depth in sorted(
            node_depths.items(), key=lambda item: (item[1], item[0][0], item[0][1]))
    ]
    return jsonify({
        'root': {'entity_type': root_type, 'entity_id': root_id},
        'direction': direction,
        'max_depth': max_depth,
        'nodes': nodes,
        'edges': [edges[key].to_dict() for key in sorted(edges)],
        'node_count': len(nodes),
        'edge_count': len(edges),
        'truncated': truncated,
    })
