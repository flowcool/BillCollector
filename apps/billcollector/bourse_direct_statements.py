"""Offered monthly originals in the existing authenticated source transaction.

Positions select a form option; they never identify a document. The tracker
keeps account/family/date identity and compares conservative PDF revisions.
"""
import hashlib
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from . import bourse_direct as source
from .notice import source_digest
from .throttle import request_interval

PAGE = '/priv/new/releves.php'
POLICY = {'query_parameters': ['nc', 'type', 'dt']}
MONTHS = ('janvier', 'février', 'mars', 'avril', 'mai', 'juin',
          'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre')


def _label(text):
    return ' '.join(text.split())


def _period(text):
    match = re.fullmatch(r'([^ ]+) ([0-9]{4})', _label(text).casefold())
    if not match or match[1] not in MONTHS or int(match[2]) < 1:
        raise source.SourceError('Bourse Direct offered statement period is invalid')
    return int(match[2]), MONTHS.index(match[1]) + 1


def _inventory(frame, account, kind, notice_account):
    document = source.Document(frame.content())
    selected = source._account(document, kind)
    if (selected['value'] != account or not selected['selected']
            or sum(item['selected'] for item in document.accounts) != 1
            or _label(selected['text']) != _label(notice_account['text'])):
        raise source.SourceError('Bourse Direct statement account identity changed')
    form = frame.locator('form#frmRlv')
    if form.count() != 1:
        raise source.SourceError('Bourse Direct statement form is ambiguous')
    data = form.evaluate('''form => ({
        method: form.method, action: form.action,
        fields: Array.from(new FormData(form).entries()),
        selectors: form.querySelectorAll('select[name="RlvCombo"]').length,
        periods: Array.from(form.querySelectorAll('select[name="RlvCombo"] option'),
            option => ({value: option.value, text: option.text, selected: option.selected}))
    })''')
    parts = urlsplit(data['action'])
    if (data['method'].lower() != 'post' or parts.scheme + '://' + parts.netloc != source.ORIGIN
            or parts.path != PAGE or parts.query or parts.fragment or data['selectors'] != 1):
        raise source.SourceError('Bourse Direct statement form target is not proven')
    periods = data['periods']
    if not periods or len(periods) > 120:
        raise source.SourceError('Bourse Direct offered statement list is invalid')
    values = [item['value'] for item in periods]
    parsed = [_period(item['text']) for item in periods]
    selected_periods = [item for item in periods if item['selected']]
    if (any(not re.fullmatch(r'[0-9]+', value) for value in values)
            or len(set(values)) != len(values) or len(set(parsed)) != len(parsed)
            or len(selected_periods) != 1
            or sorted(data['fields']) != sorted([['nc', account], ['RlvCombo', selected_periods[0]['value']]])):
        raise source.SourceError('Bourse Direct statement period identity is ambiguous')
    return document, list(zip(parsed, values)), _period(selected_periods[0]['text'])


def collect_statements(ctx, options, notice_account, notice_count, save_download,
                       remaining, wait, arm_post, pending_key, completed):
    page, context = ctx.page, ctx.context
    link = page.locator('a[href="/fr/page/releves"]')
    if not link.count():
        raise source.SourceError('Bourse Direct statement navigation is unavailable')
    page.goto(urljoin(page.url, link.first.get_attribute('href')),
              wait_until='domcontentloaded', timeout=min(30000, remaining()))
    wait(lambda: len([f for f in page.frames if urlsplit(f.url).path == PAGE]) == 1)
    frame = [f for f in page.frames if urlsplit(f.url).path == PAGE][0]
    frame.locator('select#nc').wait_for(timeout=min(15000, remaining()))
    selected = source._account(source.Document(frame.content()), options['account_type'])
    if _label(selected['text']) != _label(notice_account['text']):
        raise source.SourceError('Bourse Direct statement account mapping is not proven')
    if not selected['selected']:
        frame.locator('select#nc').select_option(value=selected['value'], timeout=min(15000, remaining()))
        with frame.expect_navigation(wait_until='domcontentloaded', timeout=min(15000, remaining())):
            frame.locator('select#nc').evaluate('''select => {
                const f = select.form;
                if (!f || f.method.toLowerCase() !== 'get' || new URL(f.action).origin !== location.origin
                    || new URL(f.action).pathname !== location.pathname) throw Error('ACCOUNT_FORM_REFUSED');
                f.requestSubmit();
            }''')
    account = selected['value']
    _, inventory, _ = _inventory(frame, account, options['account_type'], notice_account)
    if notice_count + len(inventory) > options['max_items']:
        raise source.SourceError('Bourse Direct item budget exceeded')
    today = ctx.bcs.date_context.today
    if any(period > (today.year, today.month) for period, _ in inventory):
        raise source.SourceError('Bourse Direct offered statement is in the future')
    interval = request_interval(ctx.db, 'document', default=5)
    for period, value in inventory:
        document, current, active = _inventory(frame, account, options['account_type'], notice_account)
        if current != inventory:
            raise source.SourceError('Bourse Direct offered statement list changed')
        if active != period:
            source.wait_for_request(page, remaining, namespace='bourse-direct-document-rate', interval=interval)
            verify_submission = arm_post(source.ORIGIN + PAGE, {'nc': [account], 'RlvCombo': [value]}, frame)
            with frame.expect_navigation(wait_until='domcontentloaded', timeout=min(15000, remaining())):
                frame.locator('form#frmRlv select[name="RlvCombo"]').select_option(
                    value=value, timeout=min(15000, remaining()))
            verify_submission()
            document, current, active = _inventory(frame, account, options['account_type'], notice_account)
            if current != inventory or active != period:
                raise source.SourceError('Bourse Direct statement period did not settle')
        popups = [a for a in document.links if 'releveCompte.php' in a.get('href', '')]
        if len(popups) != 1:
            raise source.SourceError('Bourse Direct offered statement link is ambiguous')
        match = re.fullmatch(r"(?:javascript:)?ouvrePopup\('([^']+)'(?:,[^)]*)?\);?", popups[0]['href'])
        if not match:
            raise source.SourceError('Bourse Direct offered statement control changed')
        target, day = source._target(frame.url, match[1], account=account, period=period, family='statement')
        if day > today:
            raise source.SourceError('Bourse Direct offered statement date is in the future')
        key = hashlib.sha256(target.encode()).hexdigest()
        if key in completed:
            continue
        source.wait_for_request(page, remaining, namespace='bourse-direct-document-rate', interval=interval)
        html = source.Document(source._read(context, target, remaining).decode('utf-8', errors='replace'))
        links = [a['href'] for a in html.links if a['text'].strip() == 'Format PDF' and 'href' in a]
        if len(links) != 1:
            raise source.SourceError('Bourse Direct statement PDF link is not unique')
        pdf_url, pdf_day = source._target(target, links[0], account=account, period=period,
                                          pdf=True, family='statement')
        if (pdf_day != day or parse_qs(urlsplit(pdf_url).query)
                != dict(parse_qs(urlsplit(target).query), trash=['/avis.pdf'], pdf=['1'])):
            raise source.SourceError('Bourse Direct statement original identity changed')
        source.wait_for_request(page, remaining, namespace='bourse-direct-document-rate', interval=interval)
        ctx.db.set_setting(pending_key, source_digest(pdf_url, POLICY))
        body = source._read(context, pdf_url, remaining, pdf=True)
        original = source.Original(body, pdf_url, options['account_type'], day)
        original.suggested_filename = (f'BourseDirect_Statement_{options["account_type"]}_'
                                       f'{day:%Y-%m}_{day.isoformat()}.pdf')
        original.identity = POLICY
        save_download(original)
        completed.add(key)
        ctx.db.set_setting(pending_key, '')
    return len(inventory)
