from plutus_agent import backtest, db, metering


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / 'plutus.db'))
    db.init_schema(conn)
    org = db.create_org(conn, 'backtest', tier='pro')['id']
    return conn, org


def test_replay_matches_estimated_event_without_writing(tmp_path):
    conn, org = _conn(tmp_path)
    metering.record_usage(conn, org, provider='anthropic',
                          model='claude-sonnet-4-6', input_tokens=1000,
                          output_tokens=100, cache_write_tokens=50)
    before = conn.execute('SELECT COUNT(*) n FROM usage_events').fetchone()['n']
    report = backtest.replay(conn, org)
    after = conn.execute('SELECT COUNT(*) n FROM usage_events').fetchone()['n']
    assert report.passed and report.checked == 1
    assert before == after == 1


def test_replay_detects_changed_pricing_outcome(tmp_path):
    conn, org = _conn(tmp_path)
    metering.record_usage(conn, org, provider='anthropic',
                          model='claude-sonnet-4-6', input_tokens=1000,
                          output_tokens=100)
    report = backtest.replay(
        conn, org,
        pricing_overrides={'anthropic': {
            'claude-sonnet-4-6': {'input': 99, 'output': 99},
        }},
    )
    assert not report.passed and report.mismatches == 1
