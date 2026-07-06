"""Tests for restore resume state (v2 id-map format) and steps remapping."""
import json

from azdo_backup.restore import (_load_restore_state, _write_restore_state,
                                 remap_steps_comprefs)


def test_state_roundtrip(tmp_path):
    path = tmp_path / "id_map.T.json"
    _write_restore_state(path, {1: 101, 2: 102}, {1})
    id_map, done = _load_restore_state(path)
    assert id_map == {1: 101, 2: 102}
    assert done == {1}
    raw = json.loads(path.read_text())
    assert raw["format"] == 2


def test_legacy_flat_map_treated_as_fully_restored(tmp_path):
    path = tmp_path / "id_map.T.json"
    path.write_text(json.dumps({"1": 101, "2": 102}))
    id_map, done = _load_restore_state(path)
    assert id_map == {1: 101, 2: 102}
    assert done == {1, 2}  # legacy files predate pass-2 tracking


def test_missing_state_file(tmp_path):
    id_map, done = _load_restore_state(tmp_path / "nope.json")
    assert id_map == {} and done == set()


def test_remap_steps_comprefs():
    xml = ('<steps><compref id="2" ref="123"/>'
           '<step id="3"/><compref id="4" ref="999"/></steps>')
    out = remap_steps_comprefs(xml, {123: 456})
    assert 'ref="456"' in out
    assert 'ref="999"' in out  # unmapped refs left alone
    assert 'ref="123"' not in out


def test_remap_steps_comprefs_no_change():
    xml = "<steps><step id='1'/></steps>"
    assert remap_steps_comprefs(xml, {1: 2}) == xml
