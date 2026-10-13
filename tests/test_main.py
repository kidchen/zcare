from apscheduler.triggers.cron import CronTrigger


def test_scheduler_registers_jobs_in_shanghai_time(settings):
    import main
    scheduler = main.build_scheduler(settings)
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert set(jobs) == {"commute", "outing", "tv", "digest"}
    trig: CronTrigger = jobs["tv"].trigger
    assert str(trig.timezone) == "Asia/Shanghai"
    assert "day_of_week='fri'" in repr(trig) and "hour='19'" in repr(trig)


def test_location_rejects_swapped_coords():
    import pytest
    from config import ConfigError, Location
    with pytest.raises(ConfigError, match="lat,lng"):
        Location.parse("X", "家", "40.81,111.67")
