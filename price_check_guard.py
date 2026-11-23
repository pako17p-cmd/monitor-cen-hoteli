"""Durable freshness/retry guard; skipped timer runs never count as success."""
import base64
from datetime import datetime, timezone, timedelta
import json
import os
import sys
import urllib.parse
import github_state

MARKER = 'price-check-status.json'
REGULAR_CRON = '17 */2 * * *'
RETRY_CRON = '7,22,37,52 * * * *'


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def should_check(state, now, manual=False, schedule=None):
    if manual:
        return True

    # Retry trigger runs only when a previous attempt has not been confirmed.
    if schedule == RETRY_CRON:
        attempted_value = state.get('attempted_at')
        if not attempted_value:
            return False
        try:
            attempted = parse_time(attempted_value)
            success_value = state.get('successful_at')
            return not success_value or attempted > parse_time(success_value)
        except (KeyError, ValueError, TypeError, AttributeError):
            return False

    # The regular cron is the independent, authoritative two-hour trigger.
    if schedule == REGULAR_CRON:
        return True

    # Push-triggered runs retain the freshness check and can recover overdue work.
    try:
        success = parse_time(state['successful_at'])
        attempted = parse_time(state.get('attempted_at', state['successful_at']))
        return attempted > success or now - success >= timedelta(hours=2) or success > now
    except (KeyError, ValueError, TypeError, AttributeError):
        return True


def read_marker():
    path = github_state._repo_path('contents/' + MARKER)
    status, result = github_state._request('GET', path + '?ref=' + urllib.parse.quote(github_state.BRANCH, safe=''))
    if status == 404:
        return {}, None
    if status != 200:
        raise RuntimeError('Cannot read price check status')
    return json.loads(base64.b64decode(result['content'])), result['sha']


def write_marker(field, value):
    for _ in range(3):
        state, sha = read_marker()
        state[field] = value
        payload = dict(message='Update price check status', branch=github_state.BRANCH,
                       content=base64.b64encode(json.dumps(state).encode()).decode())
        if sha:
            payload['sha'] = sha
        status, _ = github_state._request('PUT', github_state._repo_path('contents/' + MARKER), payload)
        if status in (200, 201):
            return
        if status not in (409, 422):
            break
    raise RuntimeError('Cannot save price check status')


def main():
    now = datetime.now(timezone.utc)
    if sys.argv[1] == 'success':
        write_marker('successful_at', now.isoformat())
        print('Full price check and history save confirmed.')
        return
    try:
        state, _ = read_marker()
    except Exception as exc:
        print(f'Cannot verify freshness; checking prices as a precaution: {exc}')
        state = {}
    due = should_check(
        state,
        now,
        manual=os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch',
        schedule=os.environ.get('SCHEDULE_CRON'),
    )
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'run={str(due).lower()}\\n')
    if due:
        # Persist before dependencies: install failures/timeouts also need a retry.
        write_marker('attempted_at', now.isoformat())
        print('Prices are due or previous attempt was unsuccessful; starting a check.')
    else:
        print('No failed attempt is pending; skipping this retry trigger.')


if __name__ == '__main__':
    main()
