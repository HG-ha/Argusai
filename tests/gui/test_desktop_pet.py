from types import SimpleNamespace

from argusai.core.assistant.stream_hub import StreamHub
from argusai.core.settings import AppSettings, _parse
from argusai.gui.desktop_pet import DesktopPet, PetActivity, read_activity
from argusai.gui.window import WindowMixin


def test_mirrored_pet_bounds_stay_inside_right_screen_edge():
    from argusai.gui.desktop_pet import body_bounds, body_dock
    for size in (120, 240, 360):
        for state in ('idle', 'wave', 'working', 'sleep', 'release'):
            left, top, right, bottom = body_bounds(state, size)
            mirrored = body_bounds(state, size, True)
            assert mirrored == (size-right, top, size-left, bottom)
            for area in ((0, 0, 1920, 1080), (-1920, 0, 1920, 1080)):
                _, x, y = body_dock(0, 100, mirrored, area, side='right')
                assert x + mirrored[2] == area[0] + area[2] - 6
                assert y + mirrored[1] >= area[1] + 6


def run(done=False, **changes):
    return dict(run_id='r1', session_id='s1', done=done, created_at=1, **changes)


def test_pending_beats_busy_and_does_not_finish_run():
    view = PetActivity()
    state = view.update([run(working=True)], 's1', now=1)
    assert state == {'state': 'wave', 'session_id': 's1'}
    assert view.update([run(working=True)], now=2)['state'] == 'working'
    assert view.update([run()], now=3)['state'] == 'thinking'


def test_completion_error_and_rest_are_transient():
    view = PetActivity()
    view.update([run()], now=0)
    assert view.update([run(True)], now=1)['state'] == 'complete'
    assert view.update([], now=8)['state'] == 'idle'
    assert view.update([], now=182)['state'] == 'sleep'
    assert view.update([dict(run(True, failed=True), run_id='r2')], now=183)['state'] == 'alert'


def test_cancel_is_not_celebrated_and_old_results_do_not_flash():
    view = PetActivity()
    assert view.update([run(True, failed=True)], now=0)['state'] == 'idle'
    view.update([dict(run(), run_id='r2')], now=1)
    assert view.update([dict(run(True, cancelled=True), run_id='r2')], now=2)['state'] == 'idle'


def test_busy_run_wins_over_other_completed_run():
    view = PetActivity()
    view.update([run()], now=0)
    assert view.update([run(True), dict(run(working=True), run_id='r2', created_at=2)], now=1)['state'] == 'working'


def test_projection_reads_without_consuming_events():
    hub = StreamHub()
    record = hub.create('r1', 's1')
    hub.publish('r1', {'kind': 'tool_activity', 'data': {'status': 'start'}})
    api = SimpleNamespace(_assistant_sessions={'s1': SimpleNamespace(_pending={'id': 1})}, _assistant_stream_hub=hub)
    before = list(record['events'])
    snapshot, pending = read_activity(api)
    assert pending == 's1' and snapshot[0]['working']
    assert record['events'] == before and not record['done']


def test_settings_default_disabled_and_roundtrip():
    assert not _parse({}).gui_desktop_pet
    settings = AppSettings(gui_desktop_pet=True, gui_pet_x=-120, gui_pet_y=80)
    restored = _parse(settings.to_dict())
    assert (restored.gui_desktop_pet, restored.gui_pet_x, restored.gui_pet_y) == (True, -120, 80)


def test_hidden_window_is_reused_and_close_destroys_once():
    calls = []
    window = SimpleNamespace(show=lambda: calls.append('show'), hide=lambda: calls.append('hide'),
                             run_js=lambda _: None, destroy=lambda: calls.append('destroy'))
    pet = DesktopPet(SimpleNamespace())
    pet.window = window
    pet.set_enabled(False)
    pet.set_enabled(True)
    pet.close()
    pet.close()
    assert calls == ['hide', 'show', 'destroy']


def test_programmatic_pet_destroy_does_not_veto_delayed_native_close():
    api = SimpleNamespace(_quit_requested=False)
    pet = DesktopPet(api)
    pet.window = SimpleNamespace(destroy=lambda: None)

    pet.close()

    assert pet.window is None
    assert pet._closing is True
    assert pet._on_closing() is True


def test_programmatic_chat_destroy_does_not_veto_application_quit():
    api = SimpleNamespace(_quit_requested=False)
    pet = DesktopPet(api)
    pet.chat_window = SimpleNamespace(destroy=lambda: None)

    pet.close()

    assert pet.chat_window is None
    assert pet._closing is True
    assert pet._on_chat_closing() is True


def test_application_quit_allows_auxiliary_windows_to_close():
    api = SimpleNamespace(_quit_requested=True)
    pet = DesktopPet(api)
    pet.window = SimpleNamespace()
    pet.chat_window = SimpleNamespace()

    assert pet._on_closing() is True
    assert pet._on_chat_closing() is True


def test_headless_host_cannot_create_companion():
    assert not WindowMixin().set_desktop_pet_enabled(True)['ok']


def test_windows_pet_disables_backdrop_again_after_theme_change(monkeypatch):
    from argusai.gui import desktop_pet_windows as native
    calls = []
    original = lambda: calls.append('backend-mica')
    form = SimpleNamespace(update_title_bar_theme=original)
    other_form = SimpleNamespace(update_title_bar_theme=original)
    monkeypatch.setattr(native, '_disable_system_backdrop', lambda target: calls.append(('none', target)))
    assert native._install_transparent_theme(form) is original
    assert calls == [('none', form)]
    calls.clear()
    form.update_title_bar_theme()
    assert calls == ['backend-mica', ('none', form)]
    assert other_form.update_title_bar_theme is original


def test_windows_pet_repairs_layered_surface_and_hides_webview(monkeypatch):
    from argusai.gui import desktop_pet_windows as native

    class Function:
        def __init__(self, callback):
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.callback(*args)

    calls = []
    user = SimpleNamespace(
        GetWindowLongW=Function(lambda hwnd, index: 0x08000000),
        SetWindowLongW=Function(lambda hwnd, index, style: calls.append(('style', hwnd, index, style)) or 0),
        SetWindowPos=Function(lambda *args: calls.append(('position', *args)) or 1),
    )
    form = SimpleNamespace(
        Handle=SimpleNamespace(ToInt64=lambda: 123),
        webview=SimpleNamespace(Visible=True),
        ShowInTaskbar=True,
    )
    monkeypatch.setattr(native, '_disable_system_backdrop', lambda target: calls.append(('backdrop', target)))

    assert native._ensure_transparent_surface(form, user) == 123
    assert form.webview.Visible is False
    assert form.ShowInTaskbar is False
    assert ('style', 123, -20, 0x08080000) in calls
    assert any(call[0] == 'position' and call[-1] == 0x27 for call in calls)
    assert ('backdrop', form) in calls


def test_windows_pet_repairs_transparency_after_theme_change(monkeypatch):
    from argusai.gui import desktop_pet_windows as native

    calls = []
    form = SimpleNamespace(update_title_bar_theme=lambda: calls.append('backend-theme'))
    repair = lambda target: calls.append(('repair', target))
    monkeypatch.setattr(native, '_disable_system_backdrop', lambda target: calls.append(('backdrop', target)))

    native._install_transparent_theme(form, repair)
    assert calls == [('repair', form)]
    calls.clear()
    form.update_title_bar_theme()
    assert calls == ['backend-theme', ('repair', form)]
