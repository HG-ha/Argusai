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


def test_headless_host_cannot_create_companion():
    assert not WindowMixin().set_desktop_pet_enabled(True)['ok']
