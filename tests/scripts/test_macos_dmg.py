"""Run the real DMG shell workflow against isolated disk/Finder stand-ins."""
from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/macos/make_dmg.sh"
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="requires a POSIX shell")

DRIVER = r'''#!/usr/bin/env python3
import json,os,shutil,sys
from pathlib import Path
command=Path(sys.argv[0]).name
args=sys.argv[1:]
statefile=Path(os.environ['DMG_TEST_STATE'])
state=json.loads(statefile.read_text())
stdin=sys.stdin.read() if command=='osascript' else ''
with Path(os.environ['DMG_TEST_LOG']).open('a') as log:
    log.write(json.dumps({'command':command,'args':args,'stdin':stdin})+'\n')
code=0
if command=='uname':
    print('Darwin')
elif command=='mount':
    print('/dev/disk7s1 on /Volumes/ArgusAI (hfs, local)')
    if state.get('attached'):
        print('/dev/disk42s1 on '+state['mount']+' (hfs, local)')
elif command=='osascript':
    if 'set current view' in stdin and state.get('layout_failure'):
        print('original Finder layout failure',file=sys.stderr)
        code=52
elif command=='hdiutil':
    action=args[0]
    if action=='create':
        state['rw']=args[-1]
        state['stage']=args[args.index('-srcfolder')+1]
        Path(state['rw']).write_text('read-write image')
    elif action=='attach':
        state['mount']=args[args.index('-mountpoint')+1]
        state['attached']=True
        shutil.copytree(state['stage'],state['mount'],symlinks=True,dirs_exist_ok=True)
        print('/dev/disk42\tGUID_partition_scheme')
        print('/dev/disk42s1\tApple_HFS\t'+state['mount'])
        if state.get('attach_failure'):
            print('original attach failure',file=sys.stderr)
            code=37
    elif action=='info':
        if state.get('info_failure'):
            print('cannot query attached images',file=sys.stderr)
            code=11
        else:
            print('image-path      : /unrelated/user-image.dmg')
            print('/dev/disk7\tGUID_partition_scheme')
            print('/dev/disk7s1\tApple_HFS\t/Volumes/ArgusAI')
            if state.get('attached'):
                print('================================================')
                print('image-path      : '+state['rw'])
                print('/dev/disk42\tGUID_partition_scheme')
                print('/dev/disk42s1\tApple_HFS\t'+state['mount'])
    elif action=='detach':
        assert args[1]==state['mount'], 'must only detach the private mount point'
        state['detaches']=state.get('detaches',0)+1
        if state.get('detach_forever') or state['detaches']<=state.get('detach_failures',0):
            print('original detach resource busy',file=sys.stderr)
            code=16
        elif not state.get('lying_detach'):
            state['attached']=False
    elif action=='convert':
        assert not state.get('attached'), 'conversion started before confirmed detach'
        state['converts']=state.get('converts',0)+1
        failures=state.get('convert_failures',0)
        if failures==-1 or state['converts']<=failures:
            Path(args[args.index('-o')+1]).write_text('partial conversion')
            print(state.get('convert_error','hdiutil: convert failed - Resource temporarily unavailable'),file=sys.stderr)
            code=state.get('convert_code',35)
        else:
            Path(args[args.index('-o')+1]).write_text('compressed DMG')
statefile.write_text(json.dumps(state))
raise SystemExit(code)
'''


@pytest.fixture
def dmg_build(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ("uname", "hdiutil", "mount", "osascript", "SetFile", "bless", "sleep", "sync"):
        path = binary / name
        path.write_text(DRIVER, encoding="utf-8")
        path.chmod(0o755)
    app = tmp_path / "ArgusAI user's app.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/marker").write_text("app data")
    temporary = tmp_path / "temporary files"
    temporary.mkdir()
    state = tmp_path / "state.json"
    log = tmp_path / "calls.jsonl"
    output = tmp_path / "output/ArgusAI.dmg"

    def run(**settings):
        if settings.pop('existing_output',False):
            output.parent.mkdir()
            output.write_text('previous complete DMG')
        state.write_text(json.dumps(settings))
        environment = dict(os.environ, TMPDIR=str(temporary), DMG_TEST_STATE=str(state), DMG_TEST_LOG=str(log),
                           PATH=os.pathsep.join((str(binary), str(Path(sys.executable).parent), os.environ["PATH"])))
        result = subprocess.run(["bash", str(SCRIPT), str(app), str(output), "ArgusAI"],
                                env=environment, text=True, capture_output=True, timeout=30)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, json.loads(state.read_text()), calls, temporary, output

    return run


def test_dmg_closes_its_window_and_confirms_detach_without_touching_same_name_volume(dmg_build):
    result, state, calls, temporary, output = dmg_build(detach_failures=2)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "compressed DMG"
    assert state["detaches"] == 3
    assert state["converts"] == 1
    detach = next(i for i, call in enumerate(calls) if call["command"] == "hdiutil" and call["args"][0] == "detach")
    assert calls[detach - 1]["command"] == "osascript"
    assert calls[detach - 1]["args"][1] == state["mount"]
    assert all(call["args"][1] != "/Volumes/ArgusAI" for call in calls
               if call["command"] == "hdiutil" and call["args"][0] == "detach")
    assert not list(temporary.iterdir())


@pytest.mark.parametrize("settings", [{"detach_forever": True}, {"lying_detach": True}, {"info_failure": True}])
def test_dmg_never_converts_or_deletes_the_work_directory_without_confirmed_detach(dmg_build, settings):
    result, state, _, temporary, output = dmg_build(**settings)
    assert result.returncode != 0
    assert state.get("converts", 0) == 0
    assert not output.exists()
    assert list(temporary.iterdir())
    assert "Keeping temporary files" in result.stderr
    assert state.get("detaches", 0) <= 12


@pytest.mark.parametrize("error", ["Resource temporarily unavailable", "Resource busy"])
def test_dmg_retries_transient_conversion_contention_only_after_detach(dmg_build, error):
    result, state, _, temporary, output = dmg_build(convert_failures=2, convert_error=error)
    assert result.returncode == 0, result.stderr
    assert state["converts"] == 3
    assert not state["attached"]
    assert output.is_file()
    assert error in result.stderr
    assert not list(temporary.iterdir())


@pytest.mark.parametrize("error", ["No space left on device", "Invalid argument", "Permission denied",
                                  "Resource temporarily unavailable; No space left on device"])
def test_dmg_does_not_retry_deterministic_conversion_failures(dmg_build, error):
    result, state, _, temporary, _ = dmg_build(convert_failures=-1, convert_error=error, convert_code=19)
    assert result.returncode == 19
    assert state["converts"] == 1
    assert error in result.stderr
    assert not list(temporary.iterdir())


def test_dmg_transient_conversion_retry_is_bounded_and_keeps_original_error(dmg_build):
    result, state, _, temporary, _ = dmg_build(convert_failures=-1)
    assert result.returncode == 35
    assert state["converts"] == 3
    assert "Resource temporarily unavailable" in result.stderr
    assert not list(temporary.iterdir())


def test_dmg_failed_conversion_preserves_existing_complete_output(dmg_build):
    result, state, _, temporary, output = dmg_build(existing_output=True, convert_failures=-1)
    assert result.returncode == 35
    assert state['converts'] == 3
    assert output.read_text() == 'previous complete DMG'
    assert not list(temporary.iterdir())


@pytest.mark.parametrize("settings,code", [({"layout_failure": True}, 52), ({"attach_failure": True}, 37),
                                         ({"layout_failure": True, "detach_forever": True}, 52)])
def test_dmg_cleanup_preserves_original_failure_status(dmg_build, settings, code):
    result, state, _, temporary, _ = dmg_build(**settings)
    assert result.returncode == code
    assert state.get("converts", 0) == 0
    assert state.get("detaches", 0) >= 1
    assert bool(list(temporary.iterdir())) == settings.get("detach_forever", False)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("hdiutil"), reason="requires native macOS disk images and Finder")
def test_native_dmg_roundtrip_preserves_layout_and_existing_same_name_volume(tmp_path):
    def existing_user_volume():
        result = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, check=True)
        for image in plistlib.loads(result.stdout).get("images", []):
            for entity in image.get("system-entities", []):
                if entity.get("mount-point") == "/Volumes/ArgusAI":
                    return image.get("image-path"), entity.get("dev-entry")
        return None

    previous_volume = existing_user_volume()
    app = tmp_path / "ArgusAI.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": "ArgusAI", "CFBundleIdentifier": "org.argusai.dmg-fixture",
        "CFBundleExecutable": "ArgusAI", "CFBundlePackageType": "APPL",
    }))
    (app / "Contents/MacOS/ArgusAI").write_text("#!/bin/sh\nexit 99\n")
    (app / "Contents/MacOS/ArgusAI").chmod(0o755)
    (app / "Contents/fixture-marker").write_text("fixture app must never be launched")
    temporary = tmp_path / "temporary images"
    temporary.mkdir()
    output = tmp_path / "ArgusAI-test.dmg"
    result = subprocess.run(["bash", str(SCRIPT), str(app), str(output), "ArgusAI"],
                            env=dict(os.environ, TMPDIR=str(temporary)), capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.stat().st_size > 0
    assert not list(temporary.iterdir())
    subprocess.run(["hdiutil", "verify", str(output)], check=True, capture_output=True, timeout=60)
    mountpoint = tmp_path / "readonly verification"
    mountpoint.mkdir()
    subprocess.run(["hdiutil", "attach", "-readonly", "-noverify", "-noautoopen",
                    "-mountpoint", str(mountpoint), str(output)], check=True, capture_output=True, timeout=60)
    try:
        assert (mountpoint / "ArgusAI.app/Contents/fixture-marker").read_text() == "fixture app must never be launched"
        assert (mountpoint / "Applications").is_symlink()
        assert os.readlink(mountpoint / "Applications") == "/Applications"
        assert (mountpoint / ".DS_Store").stat().st_size > 0
    finally:
        detached = subprocess.run(["hdiutil", "detach", str(mountpoint), "-quiet"], capture_output=True, timeout=30)
        if detached.returncode:
            subprocess.run(["hdiutil", "detach", str(mountpoint), "-force", "-quiet"],
                           check=True, capture_output=True, timeout=30)
    assert existing_user_volume() == previous_volume
