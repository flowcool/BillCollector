"""Run the actual source recipe against a private, credential-free HTTP portal."""
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import yaml

from billcollector import bourse_direct as source, config, downloads, recipes, runner
from billcollector.db import Database
from billcollector.variables import DateContext

ROOT = Path(__file__).resolve().parents[1]


def original(generation, corrected=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b'0 0 20 10 re S' if corrected else b'0 0 10 10 re S')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/CreationDate': str(generation), '/ModDate': str(generation)})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def lab(tmp_path, monkeypatch):
    data = SimpleNamespace(generation=0, corrected=False, requests=[], reservations=[],
                           refused=[], pdfs=[], wrong_pdf=False, empty=False,
                           missing_pdf=False, same_date=False, calendar_delay=0)
    db_path = tmp_path / 'proof.db'

    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def respond(self, body, content_type='text/html', headers=None):
            self.send_response(200)
            self.send_header('Content-Type', content_type + ('; charset=utf-8' if content_type == 'text/html' else ''))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else body.encode())

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            data.requests.append(('POST', self.path))
            with sqlite3.connect(db_path) as connection:
                data.reservations.append(connection.execute('SELECT login_pending_run_id FROM Task').fetchone()[0])
            if self.path == '/hub/auth/login':
                self.respond('<div class="code-input react-code-input">'
                             + ''.join(f'<input type="number" data-id="{i}">' for i in range(6))
                             + '''</div><input type="radio" name="devicestate" id="distrusted">
                             <script>document.querySelector('[data-id="0"]').oninput=async e=>{
                               if(e.target.value.length===6 && document.querySelector('#distrusted').checked){
                                await fetch('/hub/auth/two-factor/validate-totp',{method:'POST'});
                                await fetch('/hub/auth/login-2fa',{method:'POST'});
                                location.href='/fr/page/inventaire';}}
                             </script>''')
            elif self.path == '/hub/auth/two-factor/validate-totp':
                self.respond('{}', 'application/json')
            elif self.path == '/hub/auth/login-2fa':
                self.respond('{}', 'application/json', {'Set-Cookie': 'synthetic_session=valid; Path=/; Max-Age=3600'})
            else:
                data.refused.append(self.path)
                self.send_error(403)

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            data.requests.append(('GET', parsed.path))
            authenticated = 'synthetic_session=valid' in self.headers.get('Cookie', '')
            if parsed.path == '/fr/login' and not authenticated:
                self.respond('''<form method="post" action="/hub/auth/login">
                  <input id="bd_auth_login_type_login" name="login">
                  <input id="bd_auth_login_type_password" name="password" type="password">
                  <button>Sign in</button></form>''')
                return
            if not authenticated:
                data.refused.append(parsed.path)
                self.send_error(403)
                return
            if parsed.path in {'/fr/login', '/fr/page/inventaire', '/fr/page/avis-operes'}:
                frame = ('<iframe src="/priv/new/avis-operes.php?nc=1&tr=RO&month=1&year=2026"></iframe>'
                         if parsed.path == '/fr/page/avis-operes' else '')
                self.respond(3 * '<nav hidden><a href="/fr/deconnexion">Logout</a><a href="/fr/page/avis-operes">Notices</a></nav>' + frame)
            elif parsed.path == source.CALENDAR:
                time.sleep(data.calendar_delay)
                nc = query.get('nc', ['1'])[0]
                year, month = int(query.get('year', ['2026'])[0]), int(query.get('month', ['1'])[0])
                links = ''
                if not data.empty:
                    for day in (2, 2 if data.same_date else 4):
                        url = 'releveOpe.php?' + urlencode({'nc':nc, 'type':'RO', 'year':year, 'month':month, 'day':day, 'trash':'/avis.pdf'})
                        links += f'<a class="linkE" href="javascript:ouvrePopup(\'{url}\',\'encours\');">{day}</a>'
                else:
                    links = source.EMPTY
                py, pm = (year-1, 12) if month == 1 else (year, month-1)
                self.respond(f'''<script>function MoisPrecedent(){{location.href='avis-operes.php?tr=RO&nc={nc}&month={pm}&year={py}'}}</script>
                  <form method="get"><select id="nc" name="nc">
                  <option value="1" {'selected' if nc=='1' else ''}>SYNTHETIC001 Holder (Compte PEA)</option>
                  <option value="2" {'selected' if nc=='2' else ''}>SYNTHETIC002 Holder (Compte PEA-PME)</option>
                  </select></form><a title="Mois précédent" href="javascript:MoisPrecedent('RO',{nc},{month},{year})">Previous</a>{links}''')
            elif parsed.path == source.NOTICE:
                if query.get('pdf') == ['1']:
                    body = original(data.generation, data.corrected and query['day'] == ['2'])
                    data.pdfs.append((dict(query), body))
                    self.respond(body, 'application/pdf')
                else:
                    if data.wrong_pdf:
                        query['nc'] = ['OTHER-ACCOUNT']
                    query['pdf'] = ['1']
                    href = 'releveOpe.php?' + urlencode({k:v[0] for k,v in query.items()})
                    self.respond('No PDF' if data.missing_pdf else f'<a href="{href}">Format PDF</a>')
            else:
                data.refused.append(parsed.path)
                self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Portal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr(source, 'ORIGIN', origin)
    monkeypatch.setattr(source, '_rate_limit', lambda page, remaining: remaining())
    monkeypatch.setattr(config, 'RUN_DIR', tmp_path / 'runs')
    monkeypatch.setattr(config, 'PROFILE_ROOT', tmp_path / 'profiles')
    monkeypatch.setattr(runner, 'get_totp', lambda url: json.dumps({'data':{'data':'123456'}}))
    monkeypatch.setattr(runner, 'pause_loop', lambda *args: 'cancel')
    seen = []
    monkeypatch.setattr(downloads, 'CONSUMERS', [seen.append])
    db = Database(db_path)
    db.set_setting('download_dir', str(tmp_path / 'downloads'))
    task_id = db.create_task('BourseDirect', 'synthetic')
    text = (ROOT/'apps/recipes_playwright/recipe-pw__boursedirect.yaml').read_text().replace('https://www.boursedirect.fr', origin)
    ok, error, recipe = recipes.validate_recipe(text)
    assert ok, error

    def launch(ctx, playwright):
        ctx.context = playwright.chromium.launch_persistent_context(
            str(tmp_path/'profile'), headless=True, executable_path=playwright.chromium.executable_path,
            accept_downloads=True, service_workers='block')
        ctx.context.route('**/*', lambda route: route.continue_()
                          if urlsplit(route.request.url).netloc == urlsplit(origin).netloc else route.abort())
        ctx.page = ctx.context.new_page()
        ctx.bcs.page = ctx.page

    monkeypatch.setattr(runner, '_launch_browser', launch)

    def run(start=date(2026, 1, 1), mode="all"):
        source.select_mode(recipe, mode)
        run_id = db.create_run(task_id)
        ctx = runner._RunContext(db.get_task(task_id), run_id, db, False,
                                 {'user':'synthetic', 'password':'invented', 'otp':'123456'}, 'http://127.0.0.1:9')
        ctx.bcs.date_context = DateContext(date(2026, 1, 15), start)
        result = runner._run_recipe(ctx, recipe)
        if result[1]:
            print('SYNTHETIC_FAILURE', [row['error'] for row in db.list_page_status(run_id)])
        return result, db.list_downloads(run_id=run_id)

    data.run, data.db, data.seen, data.recipe = run, db, seen, recipe
    data.task_id = task_id
    yield data
    server.shutdown()
    thread.join(5)
    server.server_close()
    db.close()
    assert not thread.is_alive()
    assert not data.refused


@pytest.mark.parametrize("mode", ["all", "incremental"])
def test_actual_recipe_login_originals_regenerated_repeat_and_correction(lab, mode):
    rows = []
    for generation in range(3):
        lab.generation = generation
        lab.corrected = generation == 2
        (files, aborted, outcome), records = lab.run(mode=mode)
        assert not aborted and outcome is None
        assert len(files) == len(records) == 2
        rows.extend(records)
    assert [r['duplicate_of_id'] is not None for r in rows] == [False, False, True, True, False, True]
    assert len(lab.seen) == 3
    assert len([r for r in lab.requests if r[0]=='POST']) == 3
    assert all(lab.reservations)
    assert lab.db.get_task(lab.task_id)['login_failures'] == 0
    for record in rows:
        assert Path(record['saved_path']).read_bytes().startswith(b'%PDF-')
    assert Path(rows[0]['saved_path']).read_bytes() == lab.pdfs[0][1]


def test_month_replacement_and_year_rollover_keep_complete_first_month(lab):
    # The old select remains visible while the next calendar response is pending.
    lab.calendar_delay = 0.3
    (files, aborted, _), rows = lab.run(date(2025, 12, 20))
    assert not aborted
    assert len(files) == len(rows) == 4
    assert any(row['filename'].endswith('2025-12-02.pdf') for row in rows)


@pytest.mark.parametrize('fault', ['wrong_pdf', 'missing_pdf', 'same_date'])
def test_source_drift_stops_before_original_read(lab, fault):
    setattr(lab, fault, True)
    (_, aborted, _), rows = lab.run()
    assert aborted
    assert not rows and not lab.pdfs and not lab.seen


def test_empty_month_uses_existing_no_download_result(lab):
    lab.empty = True
    (files, aborted, outcome), rows = lab.run()
    assert not aborted and not rows
    assert runner.run_result(aborted, files, outcome) == 'failure'


def test_recipe_requires_exact_identity_before_browser(lab):
    recipe = lab.recipe
    recipe['services'][0]['steps'][1]['notice_identity']['query_parameters'] = ['day']
    ok, error, _ = recipes.validate_recipe(yaml.safe_dump(recipe))
    assert not ok and 'source identity policy' in error


def test_two_source_actions_are_refused_before_browser(lab):
    recipe = lab.recipe
    recipe['services'][0]['steps'].append(recipe['services'][0]['steps'][1])
    ok, error, _ = recipes.validate_recipe(yaml.safe_dump(recipe))
    assert not ok and 'one source action' in error


def test_persisted_uncertain_acquisition_never_replays(lab):
    lab.db.set_setting(f'bourse_direct_pending_{lab.task_id}', 'synthetic-digest')
    (_, aborted, _), rows = lab.run()
    assert aborted and not rows and not lab.pdfs
    assert not [r for r in lab.requests if r[0] == 'POST']


def test_persisted_login_failure_prevents_a_second_submission(lab):
    lab.db.update_task(lab.task_id, login_failures=1)
    (_, aborted, _), rows = lab.run()
    assert aborted and not rows
    assert not [r for r in lab.requests if r[0] == 'POST']


def test_rate_gate_keeps_at_least_sixty_seconds_between_dispatches(tmp_path, monkeypatch):
    # Use the actual gate, with a controlled clock; no real sleep or browser.
    ticks = [1000.0]
    sleeps = []
    monkeypatch.setattr(config, 'RUN_DIR', tmp_path)
    monkeypatch.setattr(source.time, 'time', lambda: ticks[0])
    def pump(milliseconds):
        sleeps.append(milliseconds)
        ticks[0] += milliseconds / 1000
    page = SimpleNamespace(wait_for_timeout=pump)
    source._rate_limit(page, lambda: 100_000)
    ticks[0] += 10
    source._rate_limit(page, lambda: 100_000)
    assert sleeps == [50_000]
    assert json.loads((tmp_path/'bourse-direct-rate.json').read_text())['at'] == 1060


@pytest.mark.parametrize('mode', ['all', 'incremental'])
def test_interrupted_delivery_restarts_without_resend_then_receipt_recovery(lab, monkeypatch, mode):
    from billcollector.interrupt import RunInterrupted

    effects = []
    def interrupted(record):
        effects.append(record)
        raise RunInterrupted()
    monkeypatch.setattr(downloads, 'CONSUMERS', [interrupted])
    with pytest.raises(RunInterrupted):
        lab.run(mode=mode)
    row = dict(lab.db.list_downloads(task_id=lab.task_id)[0])
    assert row['state'] == 'delivery_uncertain'
    assert Path(row['saved_path']).read_bytes() == lab.pdfs[0][1]
    # Open a new connection as a restarted daemon would. No consumer replay.
    restarted = Database(lab.db.path)
    downloads.recover_downloads(restarted)
    restarted.close()
    pdf_count = len(lab.pdfs)
    (_, aborted, _), rows = lab.run(mode=mode)
    assert aborted and not rows and len(effects) == 1 and len(lab.pdfs) == pdf_count
    receipt = {'original_sha256': row['sha256'], 'paperless_document_id': 101,
               'source': 'Bourse Direct', 'original_readback': True,
               'owner_readback': True, 'reader_readback': True}
    with pytest.raises(ValueError):
        source.reconcile_delivery(lab.db, row['id'], dict(receipt, original_sha256='wrong'))
    assert lab.db.list_downloads(task_id=lab.task_id)[0]['state'] == 'delivery_uncertain'
    source.reconcile_delivery(lab.db, row['id'], receipt)
    monkeypatch.setattr(downloads, 'CONSUMERS', [effects.append])
    (_, aborted, _), rows = lab.run(mode=mode)
    assert not aborted
    assert [r['duplicate_of_id'] is not None for r in rows] == [True, False]
    assert len(effects) == 2  # Only the second original needs delivery.
    lab.run(mode=mode)
    assert len(effects) == 2


@pytest.mark.parametrize('mode', ['all', 'incremental'])
def test_consumer_timeout_is_uncertain_and_does_not_replay(lab, monkeypatch, mode):
    effects = []
    def uncertain(record):
        effects.append(record)
        raise TimeoutError('Synthetic uncertain external effect')
    monkeypatch.setattr(downloads, 'CONSUMERS', [uncertain])
    (_, aborted, _), _ = lab.run(mode=mode)
    assert aborted and len(effects) == 1
    downloads.recover_downloads(lab.db)
    lab.run(mode=mode)
    assert len(effects) == 1
    assert lab.db.list_downloads(task_id=lab.task_id)[0]['state'] == 'delivery_uncertain'


def test_mode_override_refuses_other_provider():
    with pytest.raises(ValueError):
        source.select_mode({'services': []}, 'incremental')


@pytest.mark.parametrize('mode', ['all', 'incremental'])
def test_interrupted_before_publication_keeps_journal_and_recovers_once(lab, monkeypatch, mode):
    from billcollector.interrupt import RunInterrupted

    publish = downloads._publish
    def interrupt(row, db):
        raise RunInterrupted()
    monkeypatch.setattr(downloads, '_publish', interrupt)
    with pytest.raises(RunInterrupted):
        lab.run(mode=mode)
    row = dict(lab.db.list_downloads(task_id=lab.task_id)[0])
    assert row['state'] == 'staged'
    assert Path(row['staged_path']).read_bytes() == lab.pdfs[0][1]
    monkeypatch.setattr(downloads, '_publish', publish)
    downloads.recover_downloads(lab.db)
    downloads.recover_downloads(lab.db)
    assert len(lab.seen) == 1
    assert lab.db.list_downloads(task_id=lab.task_id)[0]['state'] == 'consumed'
    # Acquisition remains pending. A verified receipt may settle this safe crash gap.
    receipt = {'original_sha256': row['sha256'], 'paperless_document_id': 102,
               'source': 'Bourse Direct', 'original_readback': True,
               'owner_readback': True, 'reader_readback': True}
    source.reconcile_delivery(lab.db, row['id'], receipt)
    (_, aborted, _), records = lab.run(mode=mode)
    assert not aborted and len(lab.seen) == 2
    assert records[0]['duplicate_of_id'] == row['id']


def test_uncertain_notice_does_not_block_another_provider(lab, monkeypatch):
    from billcollector.interrupt import RunInterrupted

    def interrupt(record):
        raise RunInterrupted()
    monkeypatch.setattr(downloads, 'CONSUMERS', [interrupt])
    with pytest.raises(RunInterrupted):
        lab.run()
    other = lab.db.create_task('another-provider', 'synthetic')
    seen = []
    monkeypatch.setattr(downloads, 'CONSUMERS', [seen.append])
    body = original('other')
    record = downloads.track_download(source.Original(body, 'https://portal.invalid/file', 'synthetic', date(2026, 1, 2)),
                                      lab.db.create_run(other), lab.db.get_task(other), lab.db)
    assert not record['duplicate'] and len(seen) == 1
    assert lab.db.list_downloads(task_id=lab.task_id)[0]['state'] == 'delivery_uncertain'


@pytest.mark.parametrize("legacy", [False, True])
def test_failure_while_recovering_strict_notice_does_not_block_another_task(lab, monkeypatch, legacy):
    from billcollector.interrupt import RunInterrupted

    publish = downloads._publish
    monkeypatch.setattr(downloads, '_publish', lambda *args: (_ for _ in ()).throw(RunInterrupted()))
    with pytest.raises(RunInterrupted):
        lab.run()
    monkeypatch.setattr(downloads, '_publish', publish)
    if legacy:
        lab.db.conn.execute('UPDATE Download SET delivery_policy=NULL WHERE task_id=?', (lab.task_id,))
        lab.db.conn.commit()
    calls = []
    def consumer(record):
        calls.append(record['task_id'])
        if record['task_id'] == lab.task_id:
            raise TimeoutError('Synthetic uncertain recovery')
    monkeypatch.setattr(downloads, 'CONSUMERS', [consumer])
    other = lab.db.create_task('another-provider', 'synthetic')
    record = downloads.track_download(source.Original(original('other'), 'https://portal.invalid/file',
                                                      'synthetic', date(2026, 1, 2)),
                                      lab.db.create_run(other), lab.db.get_task(other), lab.db)
    assert not record['duplicate'] and calls == [lab.task_id, other]
    assert lab.db.list_downloads(task_id=lab.task_id)[0]['state'] == 'delivery_uncertain'
    assert lab.db.list_downloads(task_id=lab.task_id)[0]['delivery_policy'] == (None if legacy else 'bourse-direct')
    downloads.recover_downloads(lab.db)
    assert calls == [lab.task_id, other]
    (_, aborted, _), _ = lab.run()
    assert aborted
