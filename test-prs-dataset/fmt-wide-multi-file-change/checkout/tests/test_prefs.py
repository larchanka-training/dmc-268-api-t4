from app.prefs import service
from app.prefs.models import Preferences


def test_round_trip():
    service.save(Preferences(1, sms=True))
    assert service.load(1).sms is True


def test_quiet_hours_wrap_midnight():
    assert service.is_quiet(Preferences(1), 23)
