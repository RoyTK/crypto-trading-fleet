from monitoring.alerting.taxonomy import Severity, channels_for


def test_p0_includes_twilio_and_pings():
    chans = channels_for(Severity.P0)
    assert "twilio" in chans
    assert "discord_ping" in chans
    assert "telegram" in chans


def test_p1_pings_no_sms():
    chans = channels_for(Severity.P1)
    assert "twilio" not in chans
    assert "discord_ping" in chans
    assert "telegram" in chans


def test_p2_discord_no_ping():
    chans = channels_for(Severity.P2)
    assert chans == ["discord"]


def test_p3_digest_only():
    chans = channels_for(Severity.P3)
    assert chans == ["digest"]
