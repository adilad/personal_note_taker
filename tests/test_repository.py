"""Tests for SegmentRepository CRUD, search, and soft delete."""
from __future__ import annotations

import datetime

import pytest


@pytest.fixture
def repo_db(tmp_path):
    """Isolated in-memory DB with full schema."""
    import recorder.db.models  # noqa: F401
    from recorder.db.session import Base, make_engine, sessionmaker

    engine = make_engine(str(tmp_path / "test.db"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def seg_repo(repo_db):
    from recorder.db.repository import SegmentRepository

    return SegmentRepository(repo_db)


def _make_seg(repo, **kwargs):
    defaults = {
        "start_ts": datetime.datetime.now().isoformat(),
        "end_ts": datetime.datetime.now().isoformat(),
        "duration_sec": 30.0,
        "transcript": "Hello world this is a test transcript",
        "audio_key": "seg_test.wav",
    }
    defaults.update(kwargs)
    return repo.create(**defaults)


def test_create_segment(seg_repo):
    seg = _make_seg(seg_repo)
    assert seg.id is not None
    assert seg.word_count > 0
    assert seg.char_count > 0


def test_get_by_id(seg_repo):
    seg = _make_seg(seg_repo)
    fetched = seg_repo.get_by_id(seg.id)
    assert fetched is not None
    assert fetched.id == seg.id


def test_get_by_id_not_found(seg_repo):
    assert seg_repo.get_by_id(99999) is None


def test_exists_by_audio_key(seg_repo):
    _make_seg(seg_repo, audio_key="unique_key.wav")
    assert seg_repo.exists_by_audio_key("unique_key.wav")
    assert not seg_repo.exists_by_audio_key("other_key.wav")


def test_soft_delete(seg_repo):
    seg = _make_seg(seg_repo)
    assert seg_repo.soft_delete(seg.id)
    assert seg_repo.get_by_id(seg.id) is None  # soft-deleted, not returned


def test_update_segment(seg_repo):
    seg = _make_seg(seg_repo)
    updated = seg_repo.update(seg.id, important=True, category="meeting")
    assert updated is not None
    assert updated.important is True
    assert updated.category == "meeting"


def test_update_tags(seg_repo):
    import json

    seg = _make_seg(seg_repo)
    tags = ["project-x", "urgent"]
    seg_repo.update(seg.id, tags=json.dumps(tags))
    fetched = seg_repo.get_by_id(seg.id)
    assert fetched.get_tags() == tags


def test_list_with_date_filter(seg_repo):
    now = datetime.datetime.now()
    yesterday = (now - datetime.timedelta(days=1)).isoformat()
    _make_seg(seg_repo, start_ts=now.isoformat(), audio_key="today.wav")
    _make_seg(seg_repo, start_ts=yesterday, audio_key="yesterday.wav")

    segs = seg_repo.list(start=now.replace(hour=0, minute=0, second=0).isoformat())
    assert len(segs) == 1
    assert segs[0].audio_key == "today.wav"


def test_to_dict(seg_repo):
    seg = _make_seg(seg_repo)
    d = seg.to_dict()
    assert "id" in d
    assert "transcript" in d
    assert "tags" in d
    assert isinstance(d["tags"], list)
