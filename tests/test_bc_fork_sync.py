"""Public collection and delivery behavior across the fork synchronization."""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from billcollector import downloads, runner
from billcollector.db import Database
from billcollector.ui.downloads import build_download_rows
from tests.test_bc_archives import PDF, Download, make_zip
from tests.test_bc_collection import _Events, _Listing, _Page


@pytest.fixture
def database(tmp_path):
    db = Database(path=tmp_path / 'collector.db')
    db.set_setting('download_dir', str(tmp_path / 'originals'))
    task = {'id': db.create_task('synthetic', 'account')}
    yield db, task
    db.close()


@pytest.mark.parametrize('prefix', [b'', b'\xef\xbb\xbf'])
def test_incremental_zip_publishes_original_then_skips_known_transport(database, prefix):
    db, task = database
    transport = Path(db.path).parent / 'transport.zip'
    original = prefix + PDF
    transport.write_bytes(make_zip([('invoice.pdf', original)]))
    for expected_clicks in (['one'], []):
        page, context = _Page(), _Events()
        listing = _Listing(page, context, [{
            'data-key': 'one', 'download': Download(transport),
        }])
        run_id = db.create_run(task['id'])
        ctx = SimpleNamespace(page=page, context=context, db=db, task=task,
                              run_id=run_id, files_downloaded=[], all_items_known=False)
        records = runner._collection_actions(ctx)['download_all'](listing, {
            'key_attribute': 'data-key', 'max_items': 2, 'timeout_ms': 1000,
            'event_window_ms': 5, 'incremental': True, 'unpack': 'pdf',
        })
        assert listing.clicked == expected_clicks
        assert runner.run_result(True, db.list_downloads(run_id=run_id), False,
                                 all_items_known=ctx.all_items_known) == 'success'
        if records:
            assert Path(records[0]['saved_path']).read_bytes() == original
            assert records[0]['source_key'] == 'data-key=one'
    assert len(db.list_downloads()) == 1
    assert not list(Path(downloads.download_dir(db)).glob('*.zip'))


@pytest.mark.parametrize('state,policy,legacy', [
    ('delivery_uncertain', None, False),
    ('published', 'bourse-direct', False),
    ('failed', 'bourse-direct', False),
    ('published', None, True),
    ('unexpected', None, False),
])
def test_generic_retry_cannot_replay_a_provider_hold(database, state, policy, legacy):
    db, task = database
    run_id = db.create_run(task['id'])
    row_id = db.insert_download(run_id, task['id'], 'https://synthetic.test/original',
                                'original.pdf', 10, 'a' * 64, '/absent/original.pdf',
                                state=state, source_identity='synthetic-notice',
                                delivery_policy=policy)
    if legacy:
        db.set_setting(f"bourse_direct_pending_{task['id']}", 'synthetic-notice')
    with mock.patch.object(downloads, 'CONSUMERS', [mock.Mock()]) as consumers:
        assert downloads.retry_delivery(db, row_id) is False
        consumers[0].assert_not_called()
    assert db.get_download(row_id)['state'] == state
    assert build_download_rows(db)[0]['retryable'] is False
    if state == 'delivery_uncertain':
        assert build_download_rows(db)[0]['delivery'] == 'delivery uncertain; reconcile first'


@pytest.mark.parametrize('value,accepted', [(True, True), (False, True), ('yes', False)])
def test_empty_collection_option_passes_recipe_validation(value, accepted):
    import yaml
    from billcollector import recipes

    options = {'key_attribute': 'data-key', 'max_items': 2,
               'timeout_ms': 1000, 'event_window_ms': 5, 'allow_empty': value}
    recipe = {'services': [{'serviceName': 'synthetic', 'steps': [{'step': 1,
              'methods': [{'method': 'locator', 'arguments': [{'selector': 'a'}]},
                          {'method': 'download_all', 'arguments': [options]}]}]}]}
    assert recipes.validate_recipe(yaml.safe_dump(recipe))[0] is accepted


def test_overlong_encoded_member_name_fails_before_publication(database):
    db, task = database
    transport = Path(db.path).parent / 'transport.zip'
    transport.write_bytes(make_zip([('é' * 126 + '.pdf', PDF)]))
    page, context = _Page(), _Events()
    listing = _Listing(page, context, [{'data-key': 'one', 'download': Download(transport)}])
    ctx = SimpleNamespace(page=page, context=context, db=db, task=task,
                          run_id=db.create_run(task['id']), files_downloaded=[])
    with pytest.raises(ValueError, match='unsafe'):
        runner._collection_actions(ctx)['download_all'](listing, {
            'key_attribute': 'data-key', 'max_items': 1, 'timeout_ms': 1000,
            'event_window_ms': 5, 'unpack': 'pdf',
        })
    assert db.list_downloads() == []
