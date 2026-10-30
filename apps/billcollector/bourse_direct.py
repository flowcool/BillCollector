"""Local Bourse Direct source adapter, recovered from retained acquisition code.

No provider JavaScript is injected. Native Playwright handles login/navigation;
the browser's authenticated request context reads the offered HTML/PDF chain.
The ordinary tracker retains untouched originals and applies notice revisions.
"""
from datetime import date
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from . import config
from .notice import source_digest
from .collection import CollectionError
from .interrupt import RunInterrupted
from .login import settle_attempt

ORIGIN = 'https://www.boursedirect.fr'
CALENDAR = '/priv/new/avis-operes.php'
NOTICE = '/priv/new/releveOpe.php'
AUTH = frozenset({'/hub/auth/login', '/hub/auth/two-factor/validate-totp', '/hub/auth/login-2fa'})
POLICY = {'query_parameters': ['nc', 'type', 'year', 'month', 'day']}
EMPTY = "Vous n'avez pas effectué d'opération ce mois-ci."


class SourceError(CollectionError):
    """A source state is not proven; diagnostics contain no source values."""


class Document(HTMLParser):
    """Read only the account selector and offered source controls."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.accounts, self.links = [], []
        self.select_count = 0
        self._select = False
        self._option = self._anchor = None
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'select':
            self._select = values.get('id') == 'nc'
            self.select_count += int(self._select)
        if tag == 'option' and self._select:
            self._option = {'value': values.get('value'), 'selected': 'selected' in values, 'text': ''}
            self.accounts.append(self._option)
        if tag == 'a':
            self._anchor = dict(values, text='')
            self.links.append(self._anchor)

    def handle_endtag(self, tag):
        if tag == 'select':
            self._select = False
        if tag == 'option':
            self._option = None
        if tag == 'a':
            self._anchor = None

    def handle_data(self, data):
        self.text.append(data)
        if self._option is not None:
            self._option['text'] += data
        if self._anchor is not None:
            self._anchor['text'] += data


def validate_options(options):
    if not isinstance(options, dict) or set(options) - {'incremental'} != {'account_type', 'max_months', 'max_items', 'timeout_ms'}:
        raise SourceError('Bourse Direct options are invalid')
    if type(options.get('incremental', False)) is not bool:
        raise SourceError('Bourse Direct incremental flag must be boolean')
    if options['account_type'] not in {'PEA', 'PEA-PME'}:
        raise SourceError('Bourse Direct account type is unsupported')
    for name, maximum in [('max_months', 120), ('max_items', 200), ('timeout_ms', 7_200_000)]:
        if type(options[name]) is not int or not 1 <= options[name] <= maximum:
            raise SourceError('Bourse Direct bounds are invalid')


def _rate_limit(page, remaining):
    """One shared instance gate for login transactions and original-PDF reads."""
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUN_DIR / 'bourse-direct-rate.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = config.RUN_DIR / 'bourse-direct-rate.json'
        last = json.loads(path.read_text())['at'] if path.exists() else 0
        wait = max(0, last + 60 - time.time())
        if wait * 1000 >= remaining():
            raise SourceError('Bourse Direct rate wait exceeds the remaining deadline')
        if wait:
            page.wait_for_timeout(wait * 1000)
        temporary = path.with_suffix('.pending')
        with temporary.open('w') as stream:
            json.dump({'at': time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(config.RUN_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _target(base, relative, *, account, period, pdf=False):
    target = urljoin(base, relative)
    parts = urlsplit(target)
    query = parse_qs(parts.query, keep_blank_values=True)
    expected = {'nc', 'type', 'year', 'month', 'day', 'trash'} | ({'pdf'} if pdf else set())
    if (parts.scheme + '://' + parts.netloc != ORIGIN or parts.path != NOTICE
            or parts.fragment or set(query) != expected
            or any(len(v) != 1 or not v[0] for v in query.values())
            or query['nc'] != [account] or query['type'] != ['RO']
            or query['trash'] != ['/avis.pdf'] or (pdf and query['pdf'] != ['1'])):
        raise SourceError('Bourse Direct document target is not proven')
    try:
        day = date(*(int(query[key][0]) for key in ('year', 'month', 'day')))
    except (ValueError, TypeError):
        raise SourceError('Bourse Direct document date is invalid') from None
    if (day.year, day.month) != period:
        raise SourceError('Bourse Direct document month changed')
    return target, day


def _account(document, kind):
    pattern = r'\(Compte\s+' + re.escape(kind) + r'\)\s*$'
    found = [x for x in document.accounts if re.search(pattern, ' '.join(x['text'].split()), re.I)]
    if (document.select_count != 1 or len(found) != 1
            or not found[0]['value'] or not found[0]['value'].isdigit()
            or len({x['value'] for x in document.accounts}) != len(document.accounts)):
        raise SourceError('Bourse Direct account selection is ambiguous')
    return found[0]


def _calendar(document, account, period):
    selected = [x for x in document.accounts if x['selected']]
    if document.select_count != 1 or len(selected) != 1 or selected[0]['value'] != account:
        raise SourceError('Bourse Direct selected account changed')
    previous = [re.fullmatch(r"javascript:MoisPrecedent\('RO',([0-9]+),([0-9]+),([0-9]+)\)", a.get('href', ''))
                for a in document.links]
    previous = [x for x in previous if x]
    if (len(previous) != 1 or previous[0][1] != account
            or (int(previous[0][3]), int(previous[0][2])) != period):
        raise SourceError('Bourse Direct calendar month is not proven')
    offered = [a for a in document.links if 'linkE' in a.get('class', '').split()]
    if not offered and EMPTY not in ''.join(document.text):
        raise SourceError('Bourse Direct empty month is not proven')
    results = []
    for link in offered:
        match = re.fullmatch(r"(?:javascript:)?ouvrePopup\('([^']+)'(?:,[^)]*)?\);?", link.get('href', ''))
        if not match:
            raise SourceError('Bourse Direct offered notice control changed')
        results.append(_target(ORIGIN + CALENDAR, match[1], account=account, period=period))
    if len({day for _, day in results}) != len(results):
        raise SourceError('Bourse Direct offered notice identity is ambiguous')
    return results


def _read(context, target, remaining, *, pdf=False):
    response = context.request.get(target, max_redirects=0, timeout=min(30_000, remaining()))
    try:
        if response.status != 200 or response.url != target:
            raise SourceError('Bourse Direct document response is not proven')
        body = response.body()
        if not body or len(body) > 20 * 1024 * 1024:
            raise SourceError('Bourse Direct document size is invalid')
        if pdf and not body.startswith(b'%PDF-'):
            raise SourceError('Bourse Direct original PDF is not proven')
        return body
    finally:
        response.dispose()


class Original:
    def __init__(self, body, url, kind, day):
        self.body, self.url = body, url
        self.suggested_filename = f'BourseDirect_{kind}_{day.isoformat()}.pdf'

    def save_as(self, path):
        Path(path).write_bytes(self.body)


def collect(ctx, options, save_download, resolve):
    """One bounded, single-attempt source transaction; never automatic resubmission."""
    validate_options(options)
    page, context, db = ctx.page, ctx.context, ctx.db
    task_id, run_id = ctx.task['id'], ctx.run_id
    dates = ctx.bcs.date_context
    start = dates.date_from.year * 12 + dates.date_from.month - 1
    stop = dates.today.year * 12 + dates.today.month - 1
    if start > stop or stop - start + 1 > options['max_months']:
        raise SourceError('Bourse Direct month budget cannot cover the date window')
    deadline = time.monotonic() + options['timeout_ms'] / 1000

    def remaining():
        value = int((deadline - time.monotonic()) * 1000)
        if value < 1:
            raise SourceError('Bourse Direct total deadline expired')
        return value

    def authenticated():
        return (page.locator('a[href="/fr/deconnexion"]').count() > 0
                and not page.locator('#bd_auth_login_type_password').is_visible())

    def wait(predicate):
        state_deadline = time.monotonic() + min(20_000, remaining()) / 1000
        while not predicate():
            state_remaining = int((state_deadline - time.monotonic()) * 1000)
            if state_remaining < 1:
                raise SourceError('Bourse Direct expected state was not proven')
            page.wait_for_timeout(min(100, remaining(), state_remaining))

    submitted = set()
    reserved = False
    pending_key = f'bourse_direct_pending_{task_id}'
    if any(row['state'] != 'consumed' for row in db.list_downloads(task_id=task_id)):
        raise SourceError('Bourse Direct pending delivery needs operator verification')
    if db.get_setting(pending_key):
        raise SourceError('Bourse Direct prior acquisition needs operator verification')

    def guard(route):
        request = route.request
        target = urlsplit(request.url)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if (target.scheme + '://' + target.netloc != ORIGIN
                    or target.path not in AUTH or target.path in submitted):
                route.abort()
                return
            submitted.add(target.path)
        route.continue_()

    context.route('**/*', guard)
    try:
        if urlsplit(page.url).scheme + '://' + urlsplit(page.url).netloc != ORIGIN:
            raise SourceError('Bourse Direct entry origin is invalid')
        # Optional consent is handled before credential fields, using native actions.
        for label in ('Tout refuser', 'Continuer sans accepter', 'Refuser', 'Reject all'):
            button = page.get_by_role('button', name=label, exact=True)
            if button.count() == 1 and button.is_visible():
                button.click(timeout=min(15000, remaining()))
                break
        if not authenticated():
            task = db.get_task(task_id)
            if task['login_failures'] or task['login_pending_run_id']:
                raise SourceError('Bourse Direct login needs operator verification before another attempt')
            _rate_limit(page, remaining)
            if not db.reserve_login_attempt(task_id, run_id, 2):
                raise SourceError('Bourse Direct login budget refused the attempt')
            reserved = True
            password = page.locator('#bd_auth_login_type_password')
            # Direct OTP entry skips the password phase, with the same durable budget.
            otp = page.locator('.code-input.react-code-input input')
            if not otp.count():
                page.locator('#bd_auth_login_type_login').fill(resolve('{{USERNAME}}'), timeout=min(15000, remaining()))
                password.fill(resolve('{{PASSWORD}}'), timeout=min(15000, remaining()))
                password.press('Enter', timeout=min(15000, remaining()))
                wait(lambda: authenticated() or otp.count() == 6
                     or page.locator('#list-system li > a:has(img[title="TOTP"])').is_visible())
            if not authenticated():
                choice = page.locator('#list-system li > a:has(img[title="TOTP"])')
                if choice.count() == 1 and choice.is_visible():
                    choice.click(timeout=min(15000, remaining()))
                wait(lambda: otp.count() == 6)
                page.locator('#distrusted').check(timeout=min(15000, remaining()))
                code = resolve('{{OTP}}')
                if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
                    raise SourceError('Bourse Direct fresh OTP is unavailable')
                otp.first.fill(code, timeout=min(15000, remaining()))
                code = None
            wait(authenticated)
        link = page.locator('a[href="/fr/page/avis-operes"]')
        if not link.count():
            raise SourceError('Bourse Direct notice navigation is unavailable')
        # Desktop/mobile menus repeat this exact same offered href.
        target = urljoin(page.url, link.first.get_attribute('href'))
        page.goto(target, wait_until='domcontentloaded', timeout=min(30000, remaining()))
        wait(lambda: len([f for f in page.frames if urlsplit(f.url).path == CALENDAR]) == 1)
        frame = [f for f in page.frames if urlsplit(f.url).path == CALENDAR][0]
        frame.locator('select#nc').wait_for(timeout=min(15000, remaining()))
        selected = _account(Document(frame.content()), options['account_type'])
        if not selected['selected']:
            frame.locator('select#nc').select_option(value=selected['value'])
            # The observed account selector is a GET form. Submit only that form.
            with frame.expect_navigation(wait_until='domcontentloaded', timeout=min(15000, remaining())):
                frame.locator('select#nc').evaluate('''select => {
                    const f = select.form;
                    if (!f || f.method.toLowerCase() !== 'get' || new URL(f.action).origin !== location.origin
                        || new URL(f.action).pathname !== location.pathname) throw Error('ACCOUNT_FORM_REFUSED');
                    f.requestSubmit();
                }''')
        account = selected['value']
        if not db.login_succeeded(task_id, run_id):
            raise SourceError('Bourse Direct login ownership changed')
        reserved = False
        count = 0
        completed = getattr(ctx, 'bourse_direct_completed', set())
        ctx.bourse_direct_completed = completed
        for index in range(stop, start - 1, -1):
            period = (index // 12, index % 12 + 1)
            document = Document(frame.content())
            confirmed = _account(document, options['account_type'])
            if confirmed['value'] != account:
                raise SourceError('Bourse Direct source account changed')
            offered = _calendar(document, account, period)
            if count + len(offered) > options['max_items']:
                raise SourceError('Bourse Direct item budget exceeded')
            count += len(offered)
            for notice_url, day in offered:
                if day > dates.today:
                    raise SourceError('Bourse Direct offered notice is in the future')
                key = hashlib.sha256(notice_url.encode()).hexdigest()
                if key in completed:
                    continue
                notice = Document(_read(context, notice_url, remaining).decode('utf-8', errors='replace'))
                links = [a['href'] for a in notice.links if a['text'].strip() == 'Format PDF' and 'href' in a]
                if len(links) != 1:
                    raise SourceError('Bourse Direct original PDF link is not unique')
                pdf_url, pdf_day = _target(notice_url, links[0], account=account, period=period, pdf=True)
                original_query = parse_qs(urlsplit(notice_url).query)
                pdf_query = parse_qs(urlsplit(pdf_url).query)
                for field in ('year', 'month', 'day'):
                    original_query[field] = [str(int(original_query[field][0]))]
                    pdf_query[field] = [str(int(pdf_query[field][0]))]
                if pdf_day != day or pdf_query != dict(original_query, pdf=['1']):
                    raise SourceError('Bourse Direct original PDF identity changed')
                _rate_limit(page, remaining)
                db.set_setting(pending_key, source_digest(pdf_url, POLICY))
                body = _read(context, pdf_url, remaining, pdf=True)
                save_download(Original(body, pdf_url, options['account_type'], day))
                completed.add(key)
                db.set_setting(pending_key, '')
            if index != start:
                control = frame.locator('a[title="Mois précédent"]')
                if control.count() != 1:
                    raise SourceError('Bourse Direct previous-month control is ambiguous')
                with frame.expect_navigation(wait_until='domcontentloaded', timeout=min(15000, remaining())):
                    control.click(timeout=min(15000, remaining()))
                frame.locator('select#nc').wait_for(timeout=min(15000, remaining()))
        return count
    except RunInterrupted:
        raise
    except SourceError:
        raise
    except Exception:
        raise SourceError('Bourse Direct operation failed; automatic resend is disabled') from None
    finally:
        if reserved:
            settle_attempt(db, task_id, run_id)
        context.unroute('**/*', guard)


def validate_recipe(recipe):
    """Keep one provider/account owner and one automatic login per task."""
    names = [entry.get('method') for service in recipe.get('services', [])
             for step in service.get('steps', []) for entry in step.get('methods', [])]
    if 'bourse_direct' in names and (names.count('bourse_direct') != 1
                                    or len(recipe['services']) != 1 or 'login' in names):
        raise ValueError('Bourse Direct requires one service and one source action without another login block')


def select_mode(recipe, mode):
    """Override the native incremental flag for this invocation only."""
    actions = [entry for service in recipe.get('services', [])
               for step in service.get('steps', []) for entry in step.get('methods', [])
               if entry.get('method') == 'bourse_direct']
    if mode not in {'all', 'incremental'} or len(actions) != 1:
        raise ValueError('Collection mode requires one Bourse Direct action')
    actions[0]['arguments'][0]['incremental'] = mode == 'incremental'


def reconcile_delivery(db, download_id, receipt):
    """Record an Infra-verified Paperless receipt. Never send a document.

    The caller must hold the native task lock. Receipt readback is Infra's
    responsibility; this function checks its binding to the retained original.
    """
    from .downloads import sha256_of

    row = db.conn.execute('SELECT * FROM Download WHERE id=?', (download_id,)).fetchone()
    if (row is None or not row['source_identity'] or row['state'] not in {'delivery_uncertain', 'consumed'}
            or receipt.get('original_sha256') != row['sha256']
            or type(receipt.get('paperless_document_id')) is not int
            or receipt['paperless_document_id'] < 1
            or receipt.get('source') != 'Bourse Direct'
            or any(receipt.get(key) is not True for key in
                   ('original_readback', 'owner_readback', 'reader_readback'))
            or not Path(row['saved_path']).is_file()
            or sha256_of(row['saved_path']) != row['sha256']):
        raise ValueError('Verified receipt does not match an uncertain retained original')
    pending = f"bourse_direct_pending_{row['task_id']}"
    value = db.get_setting(pending)
    if (row['state'] == 'consumed' and not value) or (value and value != row['source_identity']):
        raise ValueError('Pending acquisition differs; inspect without clearing state')
    with db.conn:
        db.conn.execute("UPDATE Download SET state='consumed' WHERE id=?", (download_id,))
        db.conn.execute('INSERT OR REPLACE INTO Settings(key,value) VALUES (?,?)',
                        (f'bourse_direct_receipt_{download_id}', json.dumps(receipt, sort_keys=True)))
        db.conn.execute('INSERT OR REPLACE INTO Settings(key,value) VALUES (?,?)', (pending, ''))
