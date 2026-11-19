#!/usr/bin/env python3
"""Check the studio heartbeat and manage the corresponding GitHub issue."""

import datetime as dt
from http.client import HTTPException
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
from zoneinfo import ZoneInfo

API = 'https://api.github.com'
REF = 'refs/heads/studio-heartbeat'
REAL = 'studio-down'
DRILL = 'studio-down-drill'
BOT = 'github-actions[bot]'
SHA = re.compile(r'[0-9a-f]{40}')
UTC_DATE = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)')
MARKERS = {
    REAL: re.compile(r'<!-- studio-down: ([0-9a-f]{40}) -->'),
    DRILL: re.compile(r'<!-- studio-down-drill: ([0-9a-f]{32}) -->'),
}
EASTERN = ZoneInfo('America/New_York')
STALE_AFTER = dt.timedelta(minutes=50)
FUTURE_LIMIT = dt.timedelta(minutes=2)
RUNBOOK_LINE = ('Check the studio, its internet connection, the board snapshot job and the '
                'heartbeat push. This issue closes automatically when the checker sees a fresh report.')


class CheckError(Exception):
    """Only fixed, sanitized diagnostics may be passed to this exception."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repo, token):
        self.base = API + '/repos/' + repo
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, data=None):
        req = Request(self.base + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Authorization': 'Bearer ' + self.token,
                               'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json',
                               'Cache-Control': 'no-cache'})
        expected = 201 if method == 'POST' else 200
        try:
            with self.opener.open(req, timeout=10) as response:
                if response.status != expected:
                    raise CheckError('unexpected API status')
                return json.load(response), response.headers.get('Link', '')
        except HTTPError as exc:
            exc.close()
            raise CheckError(f'API request failed with HTTP {exc.code}') from exc
        except (OSError, HTTPException, ValueError, EOFError) as exc:
            raise CheckError('API request failed') from exc

    def list_issues(self, label):
        page = 1
        path = '/issues?' + urlencode({'state': 'all', 'labels': label, 'per_page': 100,
                                       'sort': 'created', 'direction': 'asc'})
        while True:
            issues, link = self.request('GET', path + '&page=' + str(page))
            if not isinstance(issues, list):
                raise CheckError('invalid issue page')
            yield from issues
            if not link:
                return
            # GitHub may use a numeric repository path in Link. Use the page
            # number only, and keep sending requests to the configured repo.
            links = {}
            for part in link.split(','):
                match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="(next|last|first|prev)"\s*', part)
                if not match or match[2] in links:
                    raise CheckError('invalid issue pagination')
                try:
                    url = urlsplit(match[1])
                    links[match[2]], = parse_qs(url.query)['page']
                except (KeyError, ValueError):
                    raise CheckError('invalid issue pagination') from None
                if url.scheme != 'https' or url.netloc != 'api.github.com':
                    raise CheckError('invalid issue pagination')
                if not re.fullmatch(r'[1-9][0-9]{0,8}', links[match[2]]):
                    raise CheckError('invalid issue pagination')
            if 'next' not in links:
                if 'last' in links and int(links['last']) != page:
                    raise CheckError('incomplete issue pagination')
                return
            page += 1
            if links['next'] != str(page):
                raise CheckError('incomplete issue pagination')


def read_ref(api):
    ref, _ = api.request('GET', '/git/ref/heads/studio-heartbeat')
    if isinstance(ref, dict) and ref.get('ref') == REF:
        obj = ref.get('object')
        if isinstance(obj, dict) and obj.get('type') == 'commit':
            sha = obj.get('sha')
            if isinstance(sha, str) and SHA.fullmatch(sha):
                return sha
    raise CheckError('invalid heartbeat ref')


def read_time(api, sha):
    commit, _ = api.request('GET', '/git/commits/' + sha)
    if isinstance(commit, dict) and commit.get('sha') == sha:
        committer = commit.get('committer')
        if isinstance(committer, dict):
            date = committer.get('date')
            if isinstance(date, str) and UTC_DATE.fullmatch(date):
                try:
                    return dt.datetime.fromisoformat(date.replace('Z', '+00:00'))
                except ValueError:
                    pass
    raise CheckError('invalid heartbeat committer time')


def lookup(read, *args):
    for _ in range(2):
        try:
            return read(*args)
        except CheckError:
            pass
    raise CheckError('heartbeat lookup unavailable')


def decide(heartbeat_at, now, open_alert):
    """Return the issue transition for one verified heartbeat observation."""
    age = now - heartbeat_at
    if age < -FUTURE_LIMIT:
        raise CheckError('heartbeat is more than two minutes ahead')
    if age > STALE_AFTER:
        return 'none' if open_alert else 'open'
    return 'close' if open_alert else 'none'


def marker(issue, label):
    if not isinstance(issue, dict):
        raise CheckError('invalid issue listing')
    if 'pull_request' in issue:
        return None
    labels = issue.get('labels')
    author = issue.get('user')
    if (not isinstance(labels, list)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in labels)
            or 'user' not in issue
            or (author is not None and (not isinstance(author, dict) or not isinstance(author.get('login'), str)))):
        raise CheckError('invalid issue metadata')
    names = {item['name'] for item in labels}
    other = DRILL if label == REAL else REAL
    if label not in names or other in names or author is None or author['login'] != BOT:
        return None
    body = issue.get('body')
    if body is None and 'body' in issue:
        return None
    if not isinstance(body, str):
        raise CheckError('invalid issue body')
    found = MARKERS[label].fullmatch(body.partition('\n')[0].removesuffix('\r'))
    if not found:
        return None
    if issue.get('state') not in ('open', 'closed') or not positive_number(issue.get('number')):
        raise CheckError('invalid incident state')
    return found[1]


def positive_number(number):
    return type(number) is int and number > 0


def scan(api, label):
    incidents = []
    numbers = set()
    for issue in api.list_issues(label):
        key = marker(issue, label)
        if key is not None:
            if issue['number'] in numbers:
                raise CheckError('issue listing shifted during pagination')
            numbers.add(issue['number'])
            incidents.append((key, issue))
    return incidents


def assignee_verified(issue, owner):
    assignees = issue.get('assignees')
    return isinstance(assignees, list) and any(
        isinstance(user, dict) and isinstance(user.get('login'), str)
        and user['login'].casefold() == owner.casefold() for user in assignees
    )


def reconcile(api, label, key, state, number=None):
    print('issue write response uncertain; refreshing issue state')
    for _ in range(2):
        try:
            matches = [issue for recorded, issue in scan(api, label)
                       if recorded == key and (number is None or issue['number'] == number)]
        except CheckError:
            continue
        if len(matches) > 1:
            break
        if matches and matches[0]['state'] == state:
            return matches[0]
    raise CheckError('issue write unresolved; hub reconciliation required')


def create(api, label, key, owner, title, details):
    payload = {'title': title, 'body': f'<!-- {label}: {key} -->\n\n{details}',
               'labels': [label], 'assignees': [owner]}
    try:
        issue, _ = api.request('POST', '/issues', payload)
        if marker(issue, label) != key or issue['state'] != 'open':
            raise CheckError('unexpected create response')
    except CheckError:
        issue = reconcile(api, label, key, 'open')
    if not assignee_verified(issue, owner):
        print(f'opened issue #{issue["number"]}')
        raise CheckError('issue assignment unverified; hub reconciliation required')
    print(f'opened issue #{issue["number"]}; assignee verified')


def close(api, label, key, number):
    try:
        issue, _ = api.request('PATCH', f'/issues/{number}', {'state': 'closed'})
        if marker(issue, label) != key or issue['state'] != 'closed' or issue['number'] != number:
            raise CheckError('unexpected close response')
    except CheckError:
        reconcile(api, label, key, 'closed', number)
    print(f'closed issue #{number}')


def check(api, owner, clock):
    sha = lookup(read_ref, api)
    heartbeat_at = lookup(read_time, api, sha)
    decide(heartbeat_at, clock(), False)  # Reject an invalid timestamp before listing issues.
    incidents = scan(api, REAL)
    active = [(key, issue) for key, issue in incidents if issue['state'] == 'open']
    if len(active) > 1:
        raise CheckError('multiple open alerts; hub reconciliation required')
    recorded = {key for key, _ in incidents}

    for attempt in range(2):
        now = clock()
        if heartbeat_at > now:
            print('heartbeat is ahead of the checker clock')
        action = decide(heartbeat_at, now, bool(active))
        if action == 'none' or (action == 'open' and sha in recorded):
            print('no issue change')
            return
        current = lookup(read_ref, api)
        if sha == current:
            action = decide(heartbeat_at, clock(), bool(active))
            break
        if attempt:
            raise CheckError('heartbeat ref kept moving')
        sha = current
        heartbeat_at = lookup(read_time, api, sha)
    if action == 'close':
        key, issue = active[0]
        close(api, REAL, key, issue['number'])
    elif action == 'open':
        local = heartbeat_at.astimezone(EASTERN)
        title = 'Studio down — no heartbeat since ' + local.strftime('%I:%M %p').lstrip('0') + ' ET'
        details = RUNBOOK_LINE + '\n\nLast report: ' + local.isoformat(timespec='seconds') + ' (Eastern).'
        create(api, REAL, sha, owner, title, details)


def main(env=None, client=GitHub, clock=lambda: dt.datetime.now(dt.timezone.utc)):
    env = os.environ if env is None else env
    try:
        event = env.get('GITHUB_EVENT_NAME')
        mode = env.get('ALERT_MODE')
        if event == 'schedule' and not mode:
            mode = 'check'
        elif event != 'workflow_dispatch' or mode not in ('check', 'drill-open', 'drill-close'):
            raise CheckError('unexpected event or mode')
        if mode == 'check' and env.get('STUDIO_DOWN_ALERT_ARMED') != 'true':
            print('studio down alert is disarmed')
            return 0

        repo = env.get('GITHUB_REPOSITORY', '')
        owner = env.get('REPOSITORY_OWNER', '')
        token = env.get('GITHUB_TOKEN', '')
        if (not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo)
                or repo.split('/')[1] in ('.', '..')
                or owner != repo.split('/')[0] or not token):
            raise CheckError('invalid workflow configuration')

        api = client(repo, token)
        if mode == 'check':
            check(api, owner, clock)
        elif mode == 'drill-open':
            create(api, DRILL, uuid.uuid4().hex, owner, 'Test: studio down alert (drill)',
                   'Confirm the phone notification, then dispatch drill-close for this issue.')
        else:
            number = env.get('DRILL_ISSUE_NUMBER', '')
            if not re.fullmatch(r'[1-9][0-9]{0,15}', number):
                raise CheckError('drill-close requires a positive issue number')
            number = int(number)
            issue, _ = api.request('GET', f'/issues/{number}')
            key = marker(issue, DRILL)
            if key is None or issue['state'] != 'open' or issue['number'] != number:
                raise CheckError('issue is not an open studio alert drill')
            close(api, DRILL, key, number)
    except CheckError as exc:
        print(f'studio down alert: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
