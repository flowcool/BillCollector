"""Run the actual source recipe against a private, credential-free HTTP portal."""
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
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


def test_recipe_accepts_optional_statement_family_without_changing_notice_identity():
    recipe = yaml.safe_load((ROOT / 'tests/fixtures/bourse_direct.yaml').read_text())
    options = recipe['services'][0]['steps'][1]['methods'][-1]['arguments'][0]
    options['collect_statements'] = True
    ok, error, _ = recipes.validate_recipe(yaml.safe_dump(recipe))
    assert ok, error
    options['collect_statements'] = 'true'
    assert not recipes.validate_recipe(yaml.safe_dump(recipe))[0]


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
                           missing_pdf=False, same_date=False, calendar_delay=0, oldest_period=None,
                           statements=[('2025-12', '20251231'), ('2025-11', '20251128')],
                           statement_fault=None, statement_corrected=False, statement_pdfs=[], posts=[],
                           probe_posts=False)
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
            fields = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode())
            data.requests.append(('POST', self.path))
            data.posts.append((self.path, fields))
            if urlsplit(self.path).path == '/priv/new/releves.php':
                self.statements(fields)
                return
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
            if parsed.path in {'/fr/login', '/fr/page/inventaire', '/fr/page/avis-operes', '/fr/page/releves'}:
                frame = ('<iframe src="/priv/new/avis-operes.php?nc=1&tr=RO&month=1&year=2026"></iframe>'
                         if parsed.path == '/fr/page/avis-operes' else '')
                if parsed.path == '/fr/page/releves':
                    frame = '<iframe src="/priv/new/releves.php"></iframe>'
                self.respond(3 * '<nav hidden><a href="/fr/deconnexion">Logout</a><a href="/fr/page/avis-operes">Notices</a><a href="/fr/page/releves">Statements</a></nav>' + frame)
            elif parsed.path == '/priv/new/releves.php':
                self.statements(query)
            elif parsed.path == '/priv/new/releveCompte.php':
                if query.get('pdf') == ['1']:
                    body = original(data.generation, data.statement_corrected and query['dt'] == ['20251128'])
                    data.statement_pdfs.append((dict(query), body))
                    self.respond(body, 'application/pdf')
                else:
                    if data.statement_fault == 'pdf_account':
                        query['nc'] = ['1']
                    if data.statement_fault == 'pdf_family':
                        query['type'] = ['RT']
                    query.update(trash=['/avis.pdf'], pdf=['1'])
                    href = 'releveCompte.php?' + urlencode({k:v[0] for k,v in query.items()})
                    link = f'<a href="{href}">Format PDF</a>'
                    self.respond(link * (2 if data.statement_fault == 'pdf_ambiguous' else 1))
            elif parsed.path == source.CALENDAR:
                time.sleep(data.calendar_delay)
                nc = query.get('nc', ['1'])[0]
                year, month = int(query.get('year', ['2026'])[0]), int(query.get('month', ['1'])[0])
                if data.oldest_period and (year, month) < data.oldest_period:
                    year, month = 2026, 1
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

        def statements(self, fields):
            # Deliberately different nc: the selected label, not the notice nc,
            # proves the same account. Position changes when months are inserted.
            index = int(fields.get('RlvCombo', ['0'])[0])
            if data.statement_fault == 'stalled':
                index = 0
            account = '9'
            label = 'OTHER Holder' if data.statement_fault == 'account_label' else 'SYNTHETIC001 Holder'
            months = dict(enumerate(('Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin',
                                     'Juillet', 'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre'), 1))
            options = ''.join(f'<option value="{i}" {"selected" if i == index else ""}>'
                              f'{months[int(period[5:])]} {period[:4]}</option>'
                              for i, (period, _) in enumerate(data.statements))
            if data.statement_fault == 'ambiguous_period':
                options += '<option value="99">Novembre 2025</option>'
            dt = data.statements[index][1]
            if data.statement_fault == 'wrong_period':
                dt = '20240930'
            kind = 'RT' if data.statement_fault == 'wrong_family' else 'RE'
            form_nc = '1' if data.statement_fault == 'form_account' else account
            popup = f"javascript:ouvrePopup('releveCompte.php?nc={account}&dt={dt}&type={kind}','encours')"
            onchange = 'document.frmRlv.submit()'
            if data.probe_posts:
                onchange = '''Promise.all([
                  fetch('/priv/new/releves.php',{method:'POST',body:new URLSearchParams(new FormData(this.form))}),
                  fetch('/priv/new/releves.php',{method:'POST',body:'nc=OTHER&RlvCombo=1'}),
                  fetch('/priv/new/releves.php',{method:'POST',body:'nc=9&RlvCombo=1&extra=1'}),
                  fetch('/priv/new/transfer.php',{method:'POST',body:'nc=9'})
                ].map(p=>p.catch(()=>null))).then(()=>document.frmRlv.submit())'''
            if data.statement_fault == 'get_period':
                onchange = "location.href='releves.php?RlvCombo='+this.value"
            self.respond(f'''<form id="frmCompte" method="get"><select id="nc" name="nc">
              <option value="9" selected>{label} (Compte PEA)</option>
              <option value="8">SYNTHETIC002 Holder (Compte PEA-PME)</option></select></form>
              <form id="frmRlv" name="frmRlv" action="?" method="post">
              <input type="hidden" name="nc" value="{form_nc}">
              <select name="RlvCombo" onchange="{onchange}">{options}</select></form>
              <a href="{popup}">Relevé de compte</a>
              <a href="javascript:ouvrePopup('releveTitres.php?nc=9&dt={dt}&type=RT','encours')">Titres</a>''')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Portal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr(source, 'ORIGIN', origin)
    monkeypatch.setattr(source, 'wait_for_request', lambda page, remaining, **kwargs: remaining())
    monkeypatch.setattr(config, 'RUN_DIR', tmp_path / 'runs')
    monkeypatch.setattr(config, 'PROFILE_ROOT', tmp_path / 'profiles')
    monkeypatch.setattr(runner, 'get_totp', lambda url: json.dumps({'data':{'data':'123456'}}))
    monkeypatch.setattr(runner, 'pause_loop', lambda *args: 'cancel')
    seen = []
    monkeypatch.setattr(downloads, 'CONSUMERS', [seen.append])
    db = Database(db_path)
    db.set_setting('download_dir', str(tmp_path / 'downloads'))
    task_id = db.create_task('BourseDirect', 'synthetic')
    recipe_path = Path(os.environ.get('BC_TEST_BOURSE_DIRECT_RECIPE', ROOT/'tests/fixtures/bourse_direct.yaml'))
    text = recipe_path.read_text().replace('https://www.boursedirect.fr', origin)
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

    def run(start=date(2026, 1, 1), mode="all", *, full_history=False):
        source.select_mode(recipe, mode)
        run_id = db.create_run(task_id)
        ctx = runner._RunContext(db.get_task(task_id), run_id, db, False,
                                 {'user':'synthetic', 'password':'invented', 'otp':'123456'}, 'http://127.0.0.1:9')
        ctx.bcs.date_context = source.collection_dates(recipe, DateContext(date(2026, 1, 15), start),
                                                      explicit_start=not full_history)
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


def enable_statements(lab):
    lab.recipe['services'][0]['steps'][1]['methods'][-1]['arguments'][0]['collect_statements'] = True


def test_combined_full_statement_list_and_repeat_preserve_notice_bytes(lab):
    (_, aborted, _), notices = lab.run()
    assert not aborted and len(notices) == 2
    hashes = [row['sha256'] for row in notices]
    enable_statements(lab)
    (_, aborted, _), rows = lab.run()
    assert not aborted and len(rows) == 4
    assert [row['sha256'] for row in rows[:2]] == hashes
    assert all(row['duplicate_of_id'] is not None for row in rows[:2])
    statements = rows[2:]
    assert [r['filename'] for r in statements] == [
        'BourseDirect_Statement_PEA_2025-12_2025-12-31.pdf',
        'BourseDirect_Statement_PEA_2025-11_2025-11-28.pdf']
    assert all(r['source_identity'] and r['revision_sha256'] for r in statements)
    for row, (_, body) in zip(statements, lab.statement_pdfs):
        assert Path(row['saved_path']).read_bytes() == body
    assert len(lab.seen) == 4
    (_, aborted, _), rows = lab.run()
    assert not aborted and all(r['duplicate_of_id'] is not None for r in rows)
    assert len(lab.seen) == 4
    assert len([path for path, _ in lab.posts if path in source.AUTH]) == 3
    assert all(path in source.AUTH or path.rstrip('?') == '/priv/new/releves.php' for path, _ in lab.posts)


def test_statement_post_guard_allows_only_armed_native_navigation(lab):
    enable_statements(lab)
    lab.probe_posts = True
    (_, aborted, _), rows = lab.run()
    assert not aborted and len(rows) == 4
    assert [(p.rstrip('?'), fields) for p, fields in lab.posts if p not in source.AUTH] == [
        ('/priv/new/releves.php', {'nc': ['9'], 'RlvCombo': ['1']})]


@pytest.mark.parametrize('mode', ['all', 'incremental'])
def test_new_late_statement_and_corrected_revision_do_not_republish_notices(lab, mode):
    enable_statements(lab)
    (_, aborted, _), first = lab.run(mode=mode)
    assert not aborted and len(lab.seen) == 4
    lab.statements.insert(0, ('2026-01', '20260114'))
    lab.statements.append(('2025-10', '20251031'))
    lab.generation = 1  # All PDFs receive new generation metadata.
    (_, aborted, _), rows = lab.run(mode=mode)
    assert not aborted and len(rows) == 6 and len(lab.seen) == 6
    assert [r['duplicate_of_id'] is None for r in rows] == [False, False, True, False, False, True]
    lab.statement_corrected = True
    (_, aborted, _), rows = lab.run(mode=mode)
    assert not aborted and len(lab.seen) == 7
    revised = [r for r in rows if r['duplicate_of_id'] is None]
    assert len(revised) == 1 and revised[0]['source_identity'] == first[3]['source_identity']
    assert revised[0]['revision_sha256'] != first[3]['revision_sha256']
    assert Path(first[3]['saved_path']).read_bytes() == original(0)
    (_, aborted, _), rows = lab.run(mode=mode)
    assert not aborted and len(lab.seen) == 7
    assert all(r['duplicate_of_id'] is not None for r in rows)
    assert len([p for p, _ in lab.posts if p in source.AUTH]) == 3


def test_full_offered_statement_list_is_independent_of_notice_date_window(lab):
    enable_statements(lab)
    lab.statements = [(f'{year}-{month:02}', f'{year}{month:02}28')
                      for year, months in [(2025, range(12, 0, -1)), (2024, range(12, 3, -1))]
                      for month in months]
    (_, aborted, _), rows = lab.run()
    assert not aborted and len(rows) == 23
    assert len(lab.statement_pdfs) == 21
    assert {q['dt'][0] for q, _ in lab.statement_pdfs} == {dt for _, dt in lab.statements}


@pytest.mark.parametrize('fault', ['account_label', 'form_account', 'ambiguous_period',
                                  'wrong_family', 'wrong_period', 'pdf_account',
                                  'pdf_family', 'pdf_ambiguous', 'stalled', 'get_period'])
def test_statement_source_drift_never_reads_unproven_original(lab, fault):
    enable_statements(lab)
    lab.statement_fault = fault
    (_, aborted, _), rows = lab.run()
    assert aborted
    expected = 1 if fault in {'stalled', 'get_period'} else 0
    assert len(lab.statement_pdfs) == expected
    assert len(rows) == len(lab.seen) == 2 + expected
    # Notice originals remain untouched even when statement acquisition fails.
    assert [r['sha256'] for r in rows[:2]] == [downloads.sha256_of(r['saved_path']) for r in rows[:2]]


@pytest.mark.parametrize('interrupt', [False, True])
def test_uncertain_statement_delivery_stops_restart_without_resend(lab, monkeypatch, interrupt):
    from billcollector.interrupt import RunInterrupted

    enable_statements(lab)
    effects = []

    def consume(record):
        effects.append(record)
        if 'Statement' in record['filename']:
            if interrupt:
                raise RunInterrupted()
            raise TimeoutError('Synthetic uncertain statement delivery')

    monkeypatch.setattr(downloads, 'CONSUMERS', [consume])
    if interrupt:
        with pytest.raises(RunInterrupted):
            lab.run()
    else:
        assert lab.run()[0][1]
    rows = lab.db.list_downloads(task_id=lab.task_id)
    uncertain = [r for r in rows if r['state'] == 'delivery_uncertain']
    assert len(uncertain) == 1 and 'Statement' in uncertain[0]['filename']
    assert uncertain[0]['delivery_policy'] == 'bourse-direct'
    assert Path(uncertain[0]['saved_path']).read_bytes() == lab.statement_pdfs[0][1]
    before = (len(effects), len(lab.requests), len(lab.statement_pdfs))
    restarted = Database(lab.db.path)
    downloads.recover_downloads(restarted)
    restarted.close()
    assert lab.run()[0][1]
    assert (len(effects), len(lab.statement_pdfs)) == (before[0], before[2])
    assert len([p for p, _ in lab.posts if p in source.AUTH]) == 3


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
    assert runner.run_result(not aborted, rows, False, outcome) == 'failure'


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


def test_native_adapter_dispatches_separate_configured_request_gates(lab, monkeypatch):
    gates = []
    lab.db.set_setting('login_request_interval_seconds', '90')
    lab.db.set_setting('document_request_interval_seconds', '3')

    def record(page, remaining, **options):
        assert remaining() > 0
        gates.append(options)

    monkeypatch.setattr(source, 'wait_for_request', record)
    (_, aborted, _), rows = lab.run()
    assert not aborted and len(rows) == 2
    assert gates == [dict(namespace='bourse-direct-rate', interval=90),
                     dict(namespace='bourse-direct-document-rate', interval=3),
                     dict(namespace='bourse-direct-document-rate', interval=3)]
    # A retained authenticated session does not consume a login gate.
    gates.clear()
    (_, aborted, _), rows = lab.run()
    assert not aborted and len(rows) == 2
    assert gates == [dict(namespace='bourse-direct-document-rate', interval=3)] * 2


def test_invalid_gate_setting_fails_before_login_or_document_requests(lab):
    lab.db.set_setting('document_request_interval_seconds', 'not-a-delay')
    (_, aborted, _), rows = lab.run()
    assert aborted and not rows and not lab.pdfs
    assert not [r for r in lab.requests if r[0] == 'POST']


def test_all_covers_thirteen_months_but_incremental_keeps_recent_window(lab):
    options = lab.recipe['services'][0]['steps'][1]['methods'][-1]['arguments'][0]
    options['max_months'] = 13
    lab.oldest_period = (2025, 1)
    (_, aborted, _), rows = lab.run(full_history=True)
    assert not aborted and len(rows) == 26
    assert {(int(q['year'][0]), int(q['month'][0])) for q, _ in lab.pdfs} == {
        (2025, month) for month in range(1, 13)} | {(2026, 1)}
    lab.pdfs.clear()
    (_, aborted, _), rows = lab.run(mode='incremental', full_history=True)
    assert not aborted and len(rows) == 2
    assert {(int(q['year'][0]), int(q['month'][0])) for q, _ in lab.pdfs} == {(2026, 1)}
    assert all(row['duplicate_of_id'] is not None for row in rows)


def test_history_reset_to_current_month_fails_closed(lab):
    options = lab.recipe['services'][0]['steps'][1]['methods'][-1]['arguments'][0]
    options['max_months'] = 13
    lab.oldest_period = (2025, 2)
    (_, aborted, _), rows = lab.run(full_history=True)
    assert aborted and len(rows) == 24
    assert not any(int(q['year'][0]) == 2025 and int(q['month'][0]) == 1 for q, _ in lab.pdfs)


def test_explicit_start_overrides_all_and_incremental_without_changing_task_window(lab):
    options = lab.recipe['services'][0]['steps'][1]['methods'][-1]['arguments'][0]
    options['max_months'] = 13
    for mode in ('all', 'incremental'):
        (_, aborted, _), rows = lab.run(mode=mode)
        assert not aborted and len(rows) == 2
    assert lab.db.get_task(lab.task_id)['date_window_days'] == 90
