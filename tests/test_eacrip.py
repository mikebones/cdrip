"""Tests for the rip start/wait logic in :mod:`cdrip.eacwin`."""

import pytest

from cdrip import eacwin


def test_start_rip_refuses_when_the_entry_is_missing(monkeypatch):
    from cdrip import eacdrive
    monkeypatch.setattr(eacdrive, "read_menu", lambda hwnd: [])
    monkeypatch.setattr(eacdrive, "find_rip_command", lambda items: None)
    with pytest.raises(RuntimeError):
        eacwin.start_rip(1)


def test_start_rip_reports_whether_it_actually_began(monkeypatch):
    """Posting the menu command succeeds whether or not a rip starts."""
    from cdrip import eacdrive
    entry = eacdrive.MenuItem(path=("Action", "Compressed..."),
                              command_id=771, opens_dialog=True)
    monkeypatch.setattr(eacdrive, "read_menu", lambda hwnd: [entry])
    monkeypatch.setattr(eacdrive, "find_rip_command", lambda items: entry)
    monkeypatch.setattr(eacdrive, "post_command", lambda hwnd, cid: None)
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)

    monkeypatch.setattr(eacwin, "rip_in_progress", lambda pid=None: True)
    assert eacwin.start_rip(1) is True

    monkeypatch.setattr(eacwin, "rip_in_progress", lambda pid=None: False)
    assert eacwin.start_rip(1) is False


def test_wait_returns_false_when_the_rip_never_starts(monkeypatch):
    monkeypatch.setattr(eacwin, "rip_complete", lambda pid=None: False)
    monkeypatch.setattr(eacwin, "rip_in_progress", lambda pid=None: False)
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    assert eacwin.wait_for_rip(timeout=0.05, poll=0.01) is False


def test_wait_returns_true_once_a_started_rip_finishes(monkeypatch):
    # rip_complete must be stubbed too, or each loop enumerates every window
    # on the machine - which makes the test slow and, worse, dependent on
    # whether EAC happens to be running here.
    monkeypatch.setattr(eacwin, "rip_complete", lambda pid=None: False)
    states = iter([True, True, True, False])
    monkeypatch.setattr(eacwin, "rip_in_progress",
                        lambda pid=None: next(states, False))
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    assert eacwin.wait_for_rip(timeout=100, poll=0.01) is True


def test_wait_raises_if_a_running_rip_never_ends(monkeypatch):
    monkeypatch.setattr(eacwin, "rip_complete", lambda pid=None: False)
    monkeypatch.setattr(eacwin, "rip_in_progress", lambda pid=None: True)
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        eacwin.wait_for_rip(timeout=0.05, poll=0.01)


def test_progress_is_not_judged_by_io_counters():
    """CD reads go through SCSI passthrough and do not move ReadOperationCount.

    A healthy rip therefore shows a zero read delta, so any check built on
    those counters reports a working rip as idle. Guard the docstring that
    records this so the reasoning cannot quietly be dropped.
    """
    doc = eacwin.rip_in_progress.__doc__ or ""
    assert "ReadOperationCount" in doc


def test_on_tick_is_called_with_the_running_state(monkeypatch):
    monkeypatch.setattr(eacwin, "rip_complete", lambda pid=None: False)
    seen = []
    states = iter([True, False])
    monkeypatch.setattr(eacwin, "rip_in_progress",
                        lambda pid=None: next(states, False))
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    eacwin.wait_for_rip(timeout=100, poll=0.01, on_tick=seen.append)
    assert seen and seen[0] is True


def test_completion_is_detected_from_the_button_not_the_window(monkeypatch):
    """EAC leaves the dialog up after a finished rip, waiting for OK.

    A wait built on the window closing hangs forever on a rip that
    succeeded - which is what the first version of this did.
    """
    monkeypatch.setattr(eacwin, "find_dialog_containing", lambda n, pid=None: 99)
    monkeypatch.setattr(eacwin, "control", lambda parent, cid: 7)
    monkeypatch.setattr(eacwin, "get_text", lambda hwnd: "OK")
    assert eacwin.rip_complete() is True

    monkeypatch.setattr(eacwin, "get_text", lambda hwnd: "Cancel")
    assert eacwin.rip_complete() is False


def test_wait_returns_as_soon_as_the_button_says_ok(monkeypatch):
    monkeypatch.setattr(eacwin, "rip_complete", lambda pid=None: True)
    monkeypatch.setattr(eacwin, "rip_in_progress", lambda pid=None: True)
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    assert eacwin.wait_for_rip(timeout=100, poll=0.01) is True


def test_rip_complete_is_false_with_no_dialog(monkeypatch):
    monkeypatch.setattr(eacwin, "find_dialog_containing", lambda n, pid=None: None)
    assert eacwin.rip_complete() is False


def test_cue_variants_do_not_default_to_the_noncompliant_one():
    """EAC labels one variant "(Noncompliant)" in its own menu.

    A rip with no cue at all is trumpable under RED 2.2.10.7, so the cue
    matters - and picking the wrong variant would produce one that is
    itself trumpable.
    """
    import inspect
    sig = inspect.signature(eacwin.create_cue)
    assert sig.parameters["variant"].default == eacwin.MENU_CUE_CORRECTED_GAPS
    assert eacwin.MENU_CUE_CORRECTED_GAPS != eacwin.MENU_CUE_NONCOMPLIANT


def test_detect_gaps_is_required_before_a_cue_is_meaningful():
    doc = eacwin.detect_gaps.__doc__ or ""
    assert "cue" in doc.lower()


def test_gap_detection_watches_its_own_window_not_the_rip_one(monkeypatch):
    """Gap detection shows "Analyzing", not "Extracting Audio Data".

    Waiting on the extraction dialog returns instantly, so the cue then
    gets written from gap information that does not exist yet - which is
    exactly what happened.
    """
    seen = []
    monkeypatch.setattr(eacwin, "find_dialog_containing",
                        lambda needle, pid=None: seen.append(needle))
    eacwin.gaps_in_progress()
    assert seen == [eacwin.ANALYZE_DIALOG]
    assert eacwin.ANALYZE_DIALOG != eacwin.RIP_DIALOG


def test_detect_gaps_waits_for_the_window_to_appear_first(monkeypatch):
    """A slow start must not look like an instant finish."""
    from cdrip import eacdrive
    monkeypatch.setattr(eacdrive, "post_command", lambda h, c: None)
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    # Absent, absent, present, present, gone.
    states = iter([False, False, True, True, False])
    monkeypatch.setattr(eacwin, "gaps_in_progress",
                        lambda pid=None: next(states, False))
    assert eacwin.detect_gaps(1, timeout=100, poll=0.01,
                              start_timeout=1.0) is True


def test_detect_gaps_treats_an_absent_window_as_already_done(monkeypatch):
    """EAC shows nothing when gaps are already known from an earlier pass."""
    from cdrip import eacdrive
    monkeypatch.setattr(eacdrive, "post_command", lambda h, c: None)
    monkeypatch.setattr(eacwin.time, "sleep", lambda s: None)
    monkeypatch.setattr(eacwin, "gaps_in_progress", lambda pid=None: False)
    assert eacwin.detect_gaps(1, timeout=1, poll=0.01,
                              start_timeout=0.05) is True
