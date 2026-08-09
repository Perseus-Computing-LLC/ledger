import pytest

from ledger_agent import db, metering


def _org(tmp_path):
    conn = db.connect(str(tmp_path / 'ledger.db'))
    db.init_schema(conn)
    org = db.create_org(conn, 'Team', tier='team', owner_email='owner@example.com')['id']
    member = db.ensure_user(conn, org, 'member@example.com', 'Member')
    return conn, org, member


def test_user_attribution_and_seat_rollup(tmp_path):
    conn, org, member = _org(tmp_path)
    result = metering.record_usage(
        conn, org, provider='openai', model='gpt-5', cost_usd=1.25,
        input_tokens=10, user_id=member['id'])
    row = conn.execute('SELECT user_id FROM usage_events WHERE id=?',
                       (result.event_id,)).fetchone()
    assert row['user_id'] == member['id']
    summary = metering.org_summary(conn, org)
    assert summary['seats']['active'] == 2
    assert summary['by_user'][0]['key'] == 'member@example.com'


def test_user_attribution_rejects_other_org_or_inactive_user(tmp_path):
    conn, org, member = _org(tmp_path)
    other = db.create_org(conn, 'Other', tier='team')['id']
    outsider = db.ensure_user(conn, other, 'other@example.com')
    with pytest.raises(ValueError):
        metering.record_usage(conn, org, provider='openai', user_id=outsider['id'])
    db.set_user_active(conn, member['id'], org, False)
    with pytest.raises(ValueError):
        metering.record_usage(conn, org, provider='openai', user_id=member['id'])
