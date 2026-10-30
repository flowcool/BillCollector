"""Exercise real durable gates with private files and a virtual clock."""
import fcntl
import json
from types import SimpleNamespace

import pytest

from billcollector import config, throttle


@pytest.fixture
def clock(tmp_path, monkeypatch):
    ticks = [1000.0]
    sleeps = []
    monkeypatch.setattr(config, 'RUN_DIR', tmp_path)
    monkeypatch.setattr(throttle.time, 'time', lambda: ticks[0])

    def pump(milliseconds):
        sleeps.append(milliseconds)
        ticks[0] += milliseconds / 1000

    return SimpleNamespace(ticks=ticks, sleeps=sleeps, page=SimpleNamespace(wait_for_timeout=pump), root=tmp_path)


def gate(clock, namespace, interval, remaining=lambda: 100_000):
    throttle.wait_for_request(clock.page, remaining, namespace=namespace, interval=interval)


def test_login_keeps_existing_sixty_second_clock_while_documents_are_independent(clock):
    # Preserve the old timestamp so upgrading cannot reset the login budget.
    (clock.root / 'bourse-direct-rate.json').write_text(json.dumps({'at': 990}))
    gate(clock, 'bourse-direct-rate', 60)
    assert clock.sleeps == [50_000]
    for _ in range(3):
        gate(clock, 'bourse-direct-document-rate', 5)
    assert clock.sleeps == [50_000, 5_000, 5_000]
    assert json.loads((clock.root / 'bourse-direct-rate.json').read_text())['at'] == 1050
    # Documents do not shift the persisted login clock or its next eligibility.
    gate(clock, 'bourse-direct-rate', 60)
    assert clock.sleeps[-1] == 50_000


def test_many_documents_consume_only_document_intervals_and_stop_at_deadline(clock):
    end = clock.ticks[0] + 401
    def remaining():
        return int((end - clock.ticks[0]) * 1000)
    for _ in range(81):
        gate(clock, 'synthetic-document-rate', 5, remaining)
    assert sum(clock.sleeps) == 400_000
    previous = (clock.root / 'synthetic-document-rate.json').read_bytes()
    with pytest.raises(throttle.ThrottleError, match='remaining deadline'):
        gate(clock, 'synthetic-document-rate', 5, remaining)
    assert (clock.root / 'synthetic-document-rate.json').read_bytes() == previous
    assert clock.ticks[0] == 1400


def test_lock_contention_is_bounded_by_the_same_deadline(clock):
    end = clock.ticks[0] + 0.25
    with (clock.root / 'synthetic-login-rate.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(throttle.ThrottleError, match='lock wait'):
            gate(clock, 'synthetic-login-rate', 60, lambda: int((end - clock.ticks[0]) * 1000))
    assert not (clock.root / 'synthetic-login-rate.json').exists()
    assert sum(clock.sleeps) == 200


@pytest.mark.parametrize('kind,default,minimum,raw,expected', [
    ('login', 60, 60, None, 60), ('login', 60, 60, '120', 120),
    ('document', 5, .1, '', 5), ('document', 5, .1, '2.5', 2.5),
])
def test_generic_settings_accept_valid_values(kind, default, minimum, raw, expected):
    db = SimpleNamespace(get_setting=lambda name: raw)
    assert throttle.request_interval(db, kind, default=default, minimum=minimum) == expected


@pytest.mark.parametrize('raw', ['nan', 'inf', '-1', '0', '3601', 'secret-invalid', True])
def test_invalid_document_settings_fail_without_leaking_values(raw):
    db = SimpleNamespace(get_setting=lambda name: raw)
    with pytest.raises(throttle.ThrottleError) as error:
        throttle.request_interval(db, 'document', default=5)
    assert 'secret-invalid' not in str(error.value)


def test_login_configuration_cannot_shorten_the_protected_interval():
    db = SimpleNamespace(get_setting=lambda name: '5')
    with pytest.raises(throttle.ThrottleError):
        throttle.request_interval(db, 'login', default=60, minimum=60)


def test_all_uses_history_bound_while_incremental_preserves_ninety_day_context():
    from datetime import date, timedelta
    from billcollector.bourse_direct import collection_dates, select_mode
    from billcollector.variables import DateContext

    options = dict(account_type='PEA', incremental=True, max_months=13,
                   max_items=100, timeout_ms=7200000)
    recipe = {'services': [{'steps': [{'methods': [{'method': 'bourse_direct',
                                                   'arguments': [options]}]}]}]}
    today = date(2026, 10, 30)
    dates = DateContext(today, today - timedelta(days=89))
    assert collection_dates(recipe, dates) is dates
    select_mode(recipe, 'all')
    assert collection_dates(recipe, dates) == DateContext(today, date(2025, 10, 1))
    assert collection_dates(recipe, dates, explicit_start=True) is dates
    select_mode(recipe, 'incremental')
    assert collection_dates(recipe, dates) is dates
