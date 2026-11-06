from types import SimpleNamespace

from argusai.core.assistant.stream_hub import StreamHub
from argusai.gui.desktop_pet import DesktopPet, _PetBridge, snap_position
from argusai.core.settings import AppSettings, _parse
from argusai.gui.desktop_pet_chat import snapshot, send, events


def controller():
    calls = []
    hub = StreamHub()
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(get=lambda: AppSettings()),
        list_assistant_sessions=lambda: [
            {'id': 'gui', 'name': 'GUI task', 'origin': 'gui', 'is_running': True},
            {'id': 'qq', 'name': 'QQ task', 'origin': 'channel', 'session_type': 'channel'},
        ],
        get_assistant_history=lambda sid: {'history': [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': '<script>literal</script>'}, {'type': 'tool_use', 'name': 'hidden'}]},
        ]},
        start_assistant_stream=lambda params: calls.append(params) or {'ok': True, 'run_id': 'run'},
        _assistant_stream_hub=hub, _assistant_sessions={}, _app_attention_state={'open_session_ids': ['gui']},
    )
    return DesktopPet(api), calls, hub


def test_desktop_chat_filters_channels_and_preserves_text():
    pet, _, _ = controller()
    result = snapshot(pet)
    assert result['session_id'] == 'gui'
    assert [row['id'] for row in result['sessions']] == ['gui']
    assert result['messages'][-1]['text'] == '<script>literal</script>'
    assert not snapshot(pet, 'qq')['ok']


def test_send_reuses_runtime_without_changing_approval_or_model():
    pet, calls, _ = controller()
    assert send(pet, 'gui', '  hello  ')['ok']
    assert calls == [{'session_id': 'gui', 'message': 'hello'}]
    assert not send(pet, 'qq', 'message')['ok']
    assert not send(pet, 'gui', ' ')['ok']


def test_new_chat_can_send_before_first_persistence():
    pet, calls, _ = controller()
    sid = _PetBridge(pet).chat_new()['session_id']
    assert snapshot(pet, sid)['ok']
    assert send(pet, sid, 'task')['ok']
    assert calls[-1]['session_id'] == sid


def test_events_are_read_only_and_session_bound():
    pet, _, hub = controller()
    snapshot(pet)
    record = hub.create('run', 'gui')
    hub.publish('run', {'kind': 'text_chunk', 'data': {'text': 'answer'}})
    hub.publish('run', {'kind': 'tool_activity', 'data': {'tool': 'tool_search', 'status': 'start'}})
    before = list(record['events'])
    result = events(pet, 'gui', 'run')
    assert result['events'] == [{'kind': 'text', 'text': 'answer'}, {'kind': 'status', 'text': 'tool_search'}]
    assert record['events'] == before
    assert not record.get('_gc_scheduled')
    assert not events(pet, 'qq', 'run')['ok']


def test_pending_approval_is_reported_without_resolution():
    pet, calls, hub = controller()
    snapshot(pet)
    hub.create('run', 'gui')
    pending = {'id': 'approval'}
    pet.api._assistant_sessions['gui'] = SimpleNamespace(_pending=pending)
    assert events(pet, 'gui', 'run')['pending']
    assert pet.api._assistant_sessions['gui']._pending == pending
    assert calls == []


def test_click_toggles_actions_and_chat_requires_explicit_selection():
    pet, calls, _ = controller()
    opened = []
    pet.chat_visible = False
    def show_chat(**kwargs):
        opened.append(kwargs)
        pet.chat_visible = True
    pet.show_chat = show_chat
    bridge = _PetBridge(pet)
    assert bridge.interact()['ok']
    assert opened == [{'toolbar': True}] and pet.interaction_until > 0
    pet.chat_window = None
    bridge.interact()
    assert not pet.chat_visible
    bridge.chat_open()
    assert opened[-1] == {} and pet.chat_visible
    pet.chat_mode = 'chat'
    bridge.dismiss_actions()
    assert pet.chat_visible  # Stale toolbar blur must not close the chat.
    pet.chat_mode = 'toolbar'
    bridge.dismiss_actions()
    assert not pet.chat_visible
    assert calls == []


def test_snap_all_four_edges_and_free_drag():
    area = (-1920, -100, 1920, 1080)
    assert snap_position(-1900, 300, 240, 260, area) == (-1920, 300)
    assert snap_position(-260, 300, 240, 260, area) == (-240, 300)
    assert snap_position(-900, -80, 240, 260, area) == (-900, -100)
    assert snap_position(-900, 700, 240, 260, area) == (-900, 720)
    assert snap_position(-900, 300, 240, 260, area) is None


def test_snap_preference_is_opt_in_and_persistent():
    assert not AppSettings().gui_pet_snap
    assert _parse(AppSettings(gui_pet_snap=True).to_dict()).gui_pet_snap


def test_drag_uses_pointer_delta_and_keeps_direction_when_paused():
    from argusai.gui.desktop_pet import drag_direction
    assert drag_direction(-20, 3) == 'left'
    assert drag_direction(20, -3) == 'right'
    assert drag_direction(2, -20) == 'up'
    assert drag_direction(-2, 20) == 'down'
    assert drag_direction(0, 0, 'left') == 'left'


def test_companion_stays_fully_inside_work_area():
    from argusai.gui.desktop_pet import contain_position
    area = (-1920, -100, 1920, 1080)
    assert contain_position(-2400, -300, 240, 260, area) == (-1920, -100)
    assert contain_position(500, 1500, 240, 260, area) == (-240, 720)
    assert contain_position(-900, 300, 240, 260, area) == (-900, 300)


def test_windows_work_area_converts_physical_to_logical(monkeypatch):
    import ctypes
    import sys
    from argusai.gui import desktop_pet
    monkeypatch.setattr(desktop_pet.sys, 'platform', 'win32')
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(GetDpiForSystem=lambda: 144)), raising=False)
    from argusai.gui import window_geometry
    monkeypatch.setattr(window_geometry, 'get_work_area', lambda: (0, 0, 2880, 1620))
    assert desktop_pet.pet_work_area() == (0, 0, 1920, 1080)
    rect = SimpleNamespace(X=-2880, Y=0, Width=2880, Height=1620)
    screen = SimpleNamespace(FromHandle=lambda handle: SimpleNamespace(WorkingArea=rect))
    monkeypatch.setitem(sys.modules, 'System.Windows.Forms', SimpleNamespace(Screen=screen))
    window = SimpleNamespace(native=SimpleNamespace(Handle=1, _scale=1.5))
    assert desktop_pet.pet_work_area(window) == (-1920, 0, 1920, 1080)


def test_docking_uses_visible_body_and_keeps_other_monitor():
    from argusai.gui.desktop_pet import body_dock, contain_body, nearest_area
    area = (1920, -200, 2560, 1440)
    bounds = (60, 70, 180, 220)
    side, x, y = body_dock(4290, 300, bounds, area)
    assert side == 'right' and x + bounds[2] == 4474
    assert y == 300
    assert contain_body(5000, -500, bounds, area) == (4294, -264)
    assert nearest_area(2300, 100, [(0, 0, 1920, 1080), area]) == area
    assert body_dock(2800, 300, bounds, area) is None


def test_coarse_timer_does_not_slow_animation():
    from argusai.gui.desktop_pet import animation_frame
    durations = [17] * 200
    assert [animation_frame(t, durations) for t in [0, .032, .064, .096]] == [0, 1, 3, 5]
    assert animation_frame(3.4, durations) == 0


def test_all_directional_visible_bounds_fit_and_have_timelines():
    from argusai.gui.desktop_pet import _bounds_manifest, body_bounds
    for state in ['idle', 'wave', 'drag-left', 'drag-right', 'drag-up', 'drag-down']:
        left, top, right, bottom = body_bounds(state)
        assert 0 < left < right < 240 and 0 < top < bottom < 240
        assert min(_bounds_manifest()['_durations'][state]) >= 10


def test_pet_size_uses_logical_laptop_work_area():
    from argusai.gui.desktop_pet import pet_dimensions
    assert pet_dimensions((0, 0, 1920, 1080)) == (168, 182)
    assert pet_dimensions((0, 24, 1470, 880)) == (129, 139)
    assert pet_dimensions((0, 0, 960, 540)) == (112, 121)


def test_pet_scale_is_clamped_and_persisted():
    from argusai.core.settings import normalise_pet_scale
    assert normalise_pet_scale(float('nan')) == 1
    assert normalise_pet_scale(float('inf')) == 1
    assert normalise_pet_scale('invalid') == 1
    assert normalise_pet_scale(.1) == .5
    assert normalise_pet_scale(10) == 1.5
    assert _parse(AppSettings(gui_pet_scale=.75).to_dict()).gui_pet_scale == .75
    from argusai.gui.desktop_pet import pet_dimensions
    assert pet_dimensions((0, 0, 1470, 880), .75) == (129, 139)


def test_gtk_adapter_preserves_global_negative_monitor_coordinates(monkeypatch):
    from argusai.gui import desktop_pet
    monkeypatch.setattr(desktop_pet.sys, 'platform', 'linux')
    moved=[]
    gui=SimpleNamespace(__name__='webview.platforms.gtk',BrowserView=SimpleNamespace(instances={'pet':SimpleNamespace(screen=SimpleNamespace(x=-1920,y=-100))}))
    window=SimpleNamespace(gui=gui,uid='pet',move=lambda x,y:moved.append((x,y)),run_js=lambda script:None)
    desktop_pet.configure_pet_window(window)
    window.move(-1700,250)
    assert moved == [(220,350)]


def test_linux_backend_prefers_xwayland_but_honors_explicit_choice(monkeypatch):
    from argusai.gui import desktop_pet
    monkeypatch.setattr(desktop_pet.sys,'platform','linux')
    monkeypatch.setenv('DISPLAY',':0');monkeypatch.setenv('WAYLAND_DISPLAY','wayland-0')
    monkeypatch.delenv('GDK_BACKEND',raising=False);monkeypatch.delenv('QT_QPA_PLATFORM',raising=False)
    desktop_pet.prepare_pet_display_backend()
    assert desktop_pet.os.environ['GDK_BACKEND']=='x11'
    monkeypatch.setenv('GDK_BACKEND','wayland');desktop_pet.prepare_pet_display_backend()
    assert desktop_pet.os.environ['GDK_BACKEND']=='wayland'


def test_pet_frontend_calls_are_exposed_by_static_bridge():
    import re
    from pathlib import Path
    from argusai.gui.desktop_pet import _PetBridge
    assets = Path(__file__).resolve().parents[2] / 'argusai/gui/pet_assets'
    source = (assets / 'bridge.js').read_text(encoding='utf-8')
    allowed = set(re.findall(r"'([a-z_]+)'", source.split('new Set([')[1].split('])')[0]))
    calls = set()
    for name in ('chat.js', 'pet.js'):
        calls.update(re.findall(r'api\(\)\??\.([a-z_]+)\(', (assets / name).read_text(encoding='utf-8')))
    assert calls <= allowed, calls - allowed
    assert all(callable(getattr(_PetBridge, name, None)) for name in allowed)
