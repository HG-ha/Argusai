"""Restricted desktop-chat facade over the existing assistant runtime."""
from __future__ import annotations


def _rows(controller):
    rows = [row for row in controller.api.list_assistant_sessions()
            if row.get('origin', 'gui') == 'gui' and row.get('session_type') != 'channel']
    controller.chat_session_ids = controller.chat_created_ids | {str(row['id']) for row in rows}
    controller.task_names = {str(row['id']): str(row.get('name') or '') for row in rows}
    return rows


def snapshot(controller, session_id=''):
    rows = _rows(controller)
    sid = str(session_id or controller.chat_session_id or '')
    if not sid:
        opened = getattr(controller.api, '_app_attention_state', {}).get('open_session_ids') or []
        sid = next((str(row['id']) for row in rows if row['id'] in opened), '')
    if not sid and rows:
        sid = str(rows[0]['id'])
    if sid and sid not in controller.chat_session_ids:
        return {'ok': False, 'error': '请选择桌面对话'}
    controller.chat_session_id = sid
    history = controller.api.get_assistant_history(sid).get('history', []) if sid else []
    messages = []
    for item in history[-30:]:
        content = item.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(str(block.get('text', '')) for block in content if isinstance(block, dict) and block.get('type') == 'text')
        if content:
            messages.append({'role': item['role'], 'text': str(content)[-16000:]})
    hub = getattr(controller.api, '_assistant_stream_hub', None)
    active = []
    if hub is not None:
        with hub._records_lock:
            records = list(hub.records.items())
        for rid, record in records:
            if record.get('session_id') == sid and not record.get('done'):
                active.append(rid)
    return {'ok': True, 'session_id': sid, 'messages': messages,
            'snap': bool(getattr(controller.api.settings_mgr.get(), 'gui_pet_snap', False)),
            'active_run': active[-1] if active else '',
            'sessions': [{'id': row['id'], 'name': row.get('name') or 'Agent 对话',
                          'running': bool(row.get('is_running')), 'pending': bool(row.get('pending_count'))}
                         for row in rows[:50]]}


def send(controller, session_id, message):
    sid, message = str(session_id or ''), str(message or '').strip()
    _rows(controller)
    if not sid or sid not in controller.chat_session_ids:
        return {'ok': False, 'error': '请选择或新建桌面对话'}
    if not message or len(message) > 20000:
        return {'ok': False, 'error': '消息不能为空，且不能超过 20000 字'}
    # No approval flags, model overrides or runtime changes: preserve the
    # selected conversation's existing policy and exclusive run ownership.
    return controller.api.start_assistant_stream({'session_id': sid, 'message': message})


def events(controller, session_id, run_id, offset=0):
    hub = getattr(controller.api, '_assistant_stream_hub', None)
    record = hub.get(run_id) if hub is not None else None
    if session_id not in controller.chat_session_ids or record is None or record.get('session_id') != session_id:
        return {'ok': False, 'done': True, 'error': '运行记录不可用，请重新打开对话'}
    offset = max(0, int(offset))
    with record['lock']:
        raw = record['events'][offset:offset + 250]
        selected = []
        for event in raw:
            kind, data = event.get('kind'), event.get('data') or {}
            if kind == 'text_chunk':
                selected.append({'kind': 'text', 'text': str(data.get('text') or event.get('text') or '')})
            elif kind in {'stage', 'tool_activity'}:
                selected.append({'kind': 'status', 'text': str(data.get('tool') or data.get('label') or '')[:200]})
            elif kind == 'run_error':
                selected.append({'kind': 'error', 'text': str(event.get('error') or data.get('message') or '运行失败，请在主界面查看')[:500]})
        done = bool(record.get('done')) and offset + len(raw) >= len(record['events'])
    live = getattr(controller.api, '_assistant_sessions', {}).get(session_id)
    pending = bool(getattr(live, '_pending', None) or getattr(live, '_user_decision_requested', False))
    # Read only: do not consume events or schedule the shared stream's GC.
    return {'ok': True, 'events': selected, 'next': offset + len(raw), 'done': done, 'pending': pending}
