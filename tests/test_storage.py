import json
from datetime import date

from storage import BACKUP_KEEP, StateStore, backup_data


def test_corrupt_file_is_kept_aside_and_previous_copy_restored(tmp_path):
    problems = []
    s = StateStore(tmp_path / "state.json", on_problem=problems.append)
    s.set("tv_recent", ["a"])
    s.set("tv_recent", ["a", "b"])           # .bak now holds ["a"]
    (tmp_path / "state.json").write_text('{"tv_recent": ["a", "b"', encoding="utf-8")  # torn write
    assert s.recent("tv_recent") == ["a"]    # restored, not wiped
    assert len(problems) == 1 and "restored" in problems[0]
    (aside,) = tmp_path.glob("state.json.corrupt-*")
    assert aside.read_text(encoding="utf-8").startswith('{"tv_recent"')   # evidence kept
    s.set("x", 1)
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {"tv_recent": ["a"], "x": 1}


def test_corrupt_without_backup_starts_empty_and_reports(tmp_path):
    problems = []
    (tmp_path / "state.json").write_text("[]", encoding="utf-8")   # valid JSON, wrong shape
    s = StateStore(tmp_path / "state.json", on_problem=problems.append)
    assert s.get("k", "d") == "d" and "no usable backup" in problems[0]
    assert list(tmp_path.glob("state.json.corrupt-*"))


def test_missing_file_falls_back_to_backup(tmp_path):
    s = StateStore(tmp_path / "state.json")
    s.set("k", 1)
    s.set("k", 2)
    (tmp_path / "state.json").unlink()
    assert s.get("k") == 1


def test_a_failing_problem_hook_does_not_break_reads(tmp_path):
    (tmp_path / "state.json").write_text("{", encoding="utf-8")
    s = StateStore(tmp_path / "state.json", on_problem=lambda m: 1 / 0)
    assert s.get("k") is None


def test_backup_copies_data_files_and_keeps_newest_days(tmp_path):
    assert backup_data(tmp_path, date(2026, 10, 1)) is None   # nothing yet
    StateStore(tmp_path / "state.json").set("k", 1)
    (tmp_path / "activity.jsonl").write_text('{"ts": 1}\n', encoding="utf-8")
    (tmp_path / "posters").mkdir()
    for day in range(1, 11):
        dest = backup_data(tmp_path, date(2026, 10, day))
    assert sorted(p.name for p in dest.iterdir()) == ["activity.jsonl", "state.json"]
    days = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert len(days) == BACKUP_KEEP and days[-1] == "20261010" and days[0] == "20261004"


def test_backup_job_is_scheduled_nightly(settings):
    import main
    job = {j.id: j for j in main.build_scheduler(settings).get_jobs()}["backup"]
    assert "hour='3'" in repr(job.trigger) and "minute='45'" in repr(job.trigger)
