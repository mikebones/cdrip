"""An upload is not finished until a client is actually seeding it.

A release went live with 0 seeders and 1 leecher because salmon added nothing
and reported success. Every signal that should have caught it lied, so this
checks the client directly.
"""

import pytest

from cdrip import seeding


def test_infohash_matches_the_real_torrent(tmp_path):
    """Hashed from the raw bytes of the info dict, not a re-encode."""
    import hashlib
    info = b"d6:lengthi12e4:name4:teste"
    raw = b"d8:announce5:http:4:info" + info + b"e"
    p = tmp_path / "t.torrent"
    p.write_bytes(raw)
    assert seeding.infohash(str(p)) == hashlib.sha1(info).hexdigest()


def test_infohash_rejects_a_non_torrent(tmp_path):
    p = tmp_path / "x.torrent"
    p.write_bytes(b"not a torrent")
    with pytest.raises(seeding.SeedingError):
        seeding.infohash(str(p))


def _state(**kw):
    return seeding.SeedState(**kw)


def test_absent_torrent_says_so_plainly():
    s = _state(found=False)
    assert not s.seeding and not s.healthy
    assert "seeding nowhere" in str(s)


def test_seeding_is_recognised():
    s = _state(found=True, status=seeding.SEEDING, percent_done=1.0)
    assert s.seeding and s.healthy


def test_verifying_counts_as_healthy_but_not_yet_seeding():
    """It is on its way; refusing here would fail every fresh add."""
    s = _state(found=True, status=seeding.CHECKING, percent_done=0.4)
    assert s.healthy and not s.seeding


def test_stopped_is_not_healthy():
    s = _state(found=True, status=seeding.STOPPED, percent_done=1.0)
    assert not s.healthy and not s.seeding


def test_an_error_disqualifies_it():
    s = _state(found=True, status=seeding.SEEDING, percent_done=1.0,
               error="No data found!")
    assert not s.healthy
    assert "No data found!" in str(s)


def test_partial_data_is_not_seeding():
    s = _state(found=True, status=seeding.SEEDING, percent_done=0.5)
    assert not s.seeding


def test_check_reports_absent_when_the_client_returns_nothing(monkeypatch):
    monkeypatch.setattr(seeding, "_rpc",
                        lambda *a, **k: ({"arguments": {"torrents": []}}, None))
    assert seeding.check("http://x/rpc", "deadbeef").found is False


def test_check_reads_the_clients_answer(monkeypatch):
    monkeypatch.setattr(seeding, "_rpc", lambda *a, **k: ({
        "arguments": {"torrents": [
            {"status": seeding.SEEDING, "percentDone": 1.0, "errorString": ""}
        ]}}, None))
    s = seeding.check("http://x/rpc", "deadbeef", client="music-0")
    assert s.seeding
    assert "music-0" in str(s)


def test_check_survives_an_unreachable_client(monkeypatch):
    monkeypatch.setattr(seeding, "_rpc", lambda *a, **k: (None, None))
    assert seeding.check("http://x/rpc", "deadbeef").found is False
