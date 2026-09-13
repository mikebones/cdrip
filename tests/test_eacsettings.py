"""Tests for the EAC settings spec.

The Win32 reads and writes need a running EAC, so they are not exercised here.
What is testable is the part that would actually go wrong silently: the spec
itself, and the grouping/dry-run logic that decides whether anything is
written.
"""

import pytest

from cdrip import eacsettings, eacwin


def _spec(**kw):
    return eacsettings.spec(kw.pop("read_offset", 6), **kw)


def test_read_offset_is_formatted_the_way_eac_shows_it():
    """EAC's offset box reads '+6', not '6' - a bare number never matches."""
    settings = {s.key: s for s in _spec(read_offset=6)}
    assert settings["read_offset"].wanted == "+6"


def test_negative_read_offset_keeps_its_sign():
    settings = {s.key: s for s in _spec(read_offset=-582)}
    assert settings["read_offset"].wanted == "-582"


def test_read_offset_is_not_hardcoded():
    """A drive-specific value must come from the caller, never a constant."""
    a = {s.key: s for s in _spec(read_offset=6)}["read_offset"].wanted
    b = {s.key: s for s in _spec(read_offset=667)}["read_offset"].wanted
    assert a != b


def test_cache_defeat_is_expected_ticked():
    """'Drive caches audio data' ticked is what makes EAC defeat the cache."""
    settings = {s.key: s for s in _spec()}
    assert settings["defeat_cache"].wanted is True


def test_silence_trimming_must_be_off():
    """It alters the audio, so the rip stops matching AccurateRip."""
    settings = {s.key: s for s in _spec()}
    assert settings["delete_silence"].wanted is False


def test_c2_must_be_off():
    settings = {s.key: s for s in _spec()}
    assert settings["c2_pointers"].wanted is False


def test_naming_scheme_matches_the_rule():
    settings = {s.key: s for s in _spec()}
    assert settings["naming_scheme"].wanted == "%tracknr2% - %title%"
    assert "%artist%" not in settings["naming_scheme"].wanted


def test_output_dir_settings_only_appear_when_asked_for():
    without = {s.key for s in _spec()}
    assert "output_dir" not in without
    with_dir = {s.key for s in _spec(output_dir=r"C:\rips")}
    assert {"output_dir", "use_fixed_output_dir"} <= with_dir


def test_every_setting_explains_itself():
    """A settings list with no rationale rots into cargo cult."""
    for s in _spec(output_dir=r"C:\rips", encoder=r"C:\flac.exe"):
        assert s.why and len(s.why) > 20, s.key


def test_every_setting_targets_a_known_dialog():
    known = {eacwin.DIALOG_DRIVE, eacwin.DIALOG_EAC, eacwin.DIALOG_COMPRESSION}
    for s in _spec(output_dir=r"C:\rips", encoder=r"C:\flac.exe"):
        assert s.dialog in known, s.key


def test_settings_are_grouped_by_dialog_so_each_opens_once():
    grouped = eacsettings._by_dialog(_spec(output_dir=r"C:\rips"))
    assert set(grouped) == {eacwin.DIALOG_DRIVE, eacwin.DIALOG_EAC,
                            eacwin.DIALOG_COMPRESSION}
    total = sum(len(v) for v in grouped.values())
    assert total == len(_spec(output_dir=r"C:\rips"))


def test_keys_are_unique():
    keys = [s.key for s in _spec(output_dir=r"C:\rips", encoder=r"C:\flac.exe")]
    assert len(keys) == len(set(keys))


def test_result_is_not_ok_when_there_are_problems():
    r = eacsettings.Result()
    assert r.ok
    r.problems.append("something")
    assert not r.ok


def test_advisories_do_not_fail_the_gate():
    r = eacsettings.Result()
    r.advisories.append("merely recommended")
    assert r.ok


def test_describe_shows_both_values_and_the_reason():
    s = _spec()[0]
    text = s.describe("wrong")
    assert "wrong" in text and s.why[:15] in text


def test_dry_run_never_writes(monkeypatch):
    """The default must be read-only: writing to EAC's options as a side
    effect of asking a question would be a nasty surprise."""
    calls = {"write": 0, "saved": []}

    monkeypatch.setattr(eacwin, "open_options",
                        lambda main, menu, caption, pid=None: 1)
    monkeypatch.setattr(eacwin, "set_page", lambda dialog, page, **kw: 2)
    monkeypatch.setattr(eacsettings, "_read", lambda page, s: "definitely wrong")
    def _boom(page, s):
        calls["write"] += 1
    monkeypatch.setattr(eacsettings, "_write", _boom)
    monkeypatch.setattr(eacwin, "close_dialog",
                        lambda dialog, save, **kw: calls["saved"].append(save))

    result = eacsettings.verify(0, _spec())
    assert calls["write"] == 0
    assert calls["saved"] and not any(calls["saved"])
    assert result.problems


def test_apply_writes_and_saves_only_dialogs_it_touched(monkeypatch):
    saved = []
    written = []
    monkeypatch.setattr(eacwin, "open_options",
                        lambda main, menu, caption, pid=None: 1)
    monkeypatch.setattr(eacwin, "set_page", lambda dialog, page, **kw: 2)
    # Everything already correct except one setting.
    def _read(page, s):
        return "wrong" if s.key == "naming_scheme" else s.wanted
    monkeypatch.setattr(eacsettings, "_read", _read)
    monkeypatch.setattr(eacsettings, "_write",
                        lambda page, s: written.append(s.key))
    monkeypatch.setattr(eacwin, "close_dialog",
                        lambda dialog, save, **kw: saved.append(save))

    result = eacsettings.apply(0, _spec(), dry_run=False)
    assert written == ["naming_scheme"]
    # Three dialogs opened; only the one holding naming_scheme is saved.
    assert saved.count(True) == 1
    assert result.changed and not result.problems


def test_apply_reports_nothing_when_everything_is_already_right(monkeypatch):
    monkeypatch.setattr(eacwin, "open_options",
                        lambda main, menu, caption, pid=None: 1)
    monkeypatch.setattr(eacwin, "set_page", lambda dialog, page, **kw: 2)
    monkeypatch.setattr(eacsettings, "_read", lambda page, s: s.wanted)
    monkeypatch.setattr(eacwin, "close_dialog", lambda dialog, save, **kw: None)

    result = eacsettings.verify(0, _spec())
    assert result.ok
    assert result.checked == len(_spec())


def test_log_checksum_is_required():
    """The setting whose absence made a previous upload trumpable.

    EAC does not sign its log unless asked. An unsigned log cannot be
    verified, so the torrent is marked Bad/No Checksum(s) no matter how
    good the rip was - and the option is off by default.
    """
    settings = {s.key: s for s in _spec()}
    assert settings["append_log_checksum"].wanted is True
    assert "Bad/No Checksum" in settings["append_log_checksum"].why


def test_log_language_is_forced_to_english():
    settings = {s.key: s for s in _spec()}
    assert settings["log_in_english"].wanted is True
    assert "2.2.10.5" in settings["log_in_english"].why


def test_directory_comparison_ignores_a_trailing_separator():
    """EAC stores a directory with a trailing backslash whether or not one
    was written, so strict equality 'fixes' it on every run forever."""
    s = {x.key: x for x in _spec(output_dir=r"C:\rips")}["output_dir"]
    assert eacsettings.matches(s, "C:\\rips\\")
    assert eacsettings.matches(s, "C:\\rips")
    assert not eacsettings.matches(s, "C:\\other")


def test_text_comparison_ignores_surrounding_whitespace():
    s = {x.key: x for x in _spec()}["naming_scheme"]
    assert eacsettings.matches(s, "  %tracknr2% - %title%  ")


def test_checkbox_comparison_is_still_exact():
    s = {x.key: x for x in _spec()}["c2_pointers"]
    assert eacsettings.matches(s, False)
    assert not eacsettings.matches(s, True)


def test_spec_without_an_offset_still_checks_everything_else():
    """An unknown drive offset must not switch the whole gate off.

    Only the offset depends on the drive; the log checksum and the rest are
    properties of EAC. An earlier version skipped all of them when no offset
    was supplied, disabling the gate silently.
    """
    without = {s.key for s in eacsettings.spec(read_offset=None)}
    assert "read_offset" not in without
    assert "append_log_checksum" in without
    assert "secure_mode" in without
    # Everything the full spec has, less the one offset-dependent setting.
    assert len(without) == len(eacsettings.spec(read_offset=6)) - 1


def test_offset_setting_appears_only_when_an_offset_is_known():
    with_offset = {s.key for s in eacsettings.spec(read_offset=6)}
    assert "read_offset" in with_offset
