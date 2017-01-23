import io
import logging
import re
import requests
from codecs import *
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from flask import request, jsonify, json
from urllib.parse import *

from exceptionservice import app
from exceptionservice.config import *

"""
This is the base-class with views
"""

__author__ = 'Miel Donkers <miel.donkers@codecentric.nl>'

log = logging.getLogger(__name__)

_JIRA_URI_SEARCH = urljoin(JIRA_URI, '/rest/api/latest/search')
_JIRA_URI_CREATE_UPDATE = urljoin(JIRA_URI, '/rest/api/latest/issue')
_JIRA_USER_PASSWD = (JIRA_USER, JIRA_PASSWD)
_JIRA_FIELDS = ['id', 'key', 'created', 'status', 'labels', 'summary', 'description', 'environment']
_CONTENT_JSON_HEADER = {'Content-Type': 'application/json'}
_JIRA_TRANSITION_REOPEN_ID = '3'

REGEX_CAUSED_BY = re.compile(r'\W*caused\W+by', re.IGNORECASE)
REGEX_COUNT = re.compile(r'.*count:\s+(\d+)', re.IGNORECASE)


class InternalError(Exception):
    """Exception raised for all internal errors.

    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message


@app.route('/', methods=['GET', 'POST'])
def receive_exception():
    try:
        if request.method == 'POST' and request.is_json:
            return add_jira_exception(request.get_json())
        else:
            return jsonify(show_all_open_issues())
    except InternalError as err:
        log.error('Error during processing:', exc_info=err)
        return 'Error during processing; \n\t{}'.format(err), 500, {}


def show_all_open_issues():
    query = {'jql': 'project=HAMISTIRF&status in (Open,"In Progress",Reopened)&issuetype=Bevinding',
             'fields': _JIRA_FIELDS}
    resp = requests.post(_JIRA_URI_SEARCH,
                         json=query,
                         headers=_CONTENT_JSON_HEADER,
                         auth=_JIRA_USER_PASSWD)

    if resp.status_code != 200:
        raise InternalError('Could not get open Jira issues, resp code; {}'.format(resp.status_code))

    return resp.json()


def add_jira_exception(json_data):
    log.info('Received json data: {}'.format(json.dumps(json_data)))
    is_duplicate = determine_if_duplicate(json_data)
    if is_duplicate[0]:
        update_to_jira(is_duplicate[1], calculate_issue_occurrence_count(is_duplicate[3]), is_issue_closed(is_duplicate[2]))
        return 'Jira issue already exists, updated: {}'.format(is_duplicate[1])

    result = add_to_jira(get_summary_from_message(json_data), create_details_string_from_json(json_data), get_stacktrace_from_message(json_data))
    return 'Jira issue added: {}'.format(result['key']), 201, {}


def is_issue_closed(status):
    return status.lower() == 'closed' or status.lower() == 'resolved'


def calculate_issue_occurrence_count(existing_count):
    count = 1

    if existing_count is not None and len(existing_count) > 0:
        match = REGEX_COUNT.match(existing_count)
        if match:
            count = int(match.group(1)) + 1

    return 'Count: {}\nLast: {}'.format(count, datetime.now())


def determine_if_duplicate(json_data):
    exception_summary = get_summary_from_message(json_data)
    issue_list = find_existing_jira_issues(exception_summary)

    new_stacktrace = get_stacktrace_from_message(json_data)
    for issue in issue_list:
        issue_stacktrace = get_stacktrace_from_issue(issue)
        s = SequenceMatcher(lambda x: x == ' ' or x == '\n' or x == '\t',
                            new_stacktrace,
                            issue_stacktrace)

        match_ratio = s.ratio() if s.real_quick_ratio() > 0.6 else 0
        if match_ratio > 0.95 and matches_exception_throw_location(new_stacktrace, issue_stacktrace):
            log.info('\nMatch ratio: {} for stacktrace:\n{}'.format(match_ratio, issue_stacktrace))
            return True, issue['key'], issue['fields']['status']['name'], issue['fields']['environment']

    return False, ''


def sanitize_jql_query(raw_jql):
    return "project=HAMISTIRF&issuetype=Bevinding&summary ~ '%s'" % raw_jql


def find_existing_jira_issues(exception_summary, start_at=0):
    query = {'jql': sanitize_jql_query(exception_summary),
             'startAt': str(start_at),
             'fields': _JIRA_FIELDS}
    resp = requests.post(_JIRA_URI_SEARCH,
                         json=query,
                         headers=_CONTENT_JSON_HEADER,
                         auth=_JIRA_USER_PASSWD)
    if resp.status_code != 200:
        raise InternalError('Could not query Jira issues, cancel processing issue. Resp code; {}'.format(resp.status_code))

    max_results = resp.json()['maxResults']
    total_results = resp.json()['total']
    issue_list = find_existing_jira_issues(exception_summary, start_at + max_results) if total_results > start_at + max_results else list()

    return issue_list + resp.json()['issues']


def get_stacktrace_from_issue(issue):
    description = issue['fields']['description']
    description_blocks = description.split('{noformat}')
    if len(description_blocks) >= 3:
        return description_blocks[1]
    else:
        return ''


def matches_exception_throw_location(new_stacktrace, issue_stacktrace):
    line_new_stacktrace = first_line_caused_by_from_printed_stacktrace(new_stacktrace)
    line_issue_stacktrace = first_line_caused_by_from_printed_stacktrace(issue_stacktrace)

    return line_new_stacktrace == line_issue_stacktrace


def first_line_caused_by_from_printed_stacktrace(printed_stacktrace):
    lines = printed_stacktrace.splitlines()
    loc_last_causedby_line = -1
    for i in range(len(lines)):
        if REGEX_CAUSED_BY.match(lines[i]):
            loc_last_causedby_line = i

    # Split at the colon, first element of tuple contains entire string if colon not found
    exception_line = lines[loc_last_causedby_line + 1].partition(':')
    return exception_line[0]


def create_details_string_from_json(json_data):
    dict_without_stacktrace = deepcopy(json_data)
    del dict_without_stacktrace['stacktrace']

    output = ''
    for key, value in dict_without_stacktrace.items():
        output += '  {}: {}\n'.format(key, value)

    return output


def get_summary_from_message(json_data):
    # Get the original exception, which is the last in the list
    stacks = json_data['stacktrace']
    return stacks[len(stacks) - 1]['message']


def get_stacktrace_from_message(json_data):
    traces = json_data['stacktrace']
    output = io.StringIO()
    for trace in traces:
        output.write('Caused by: {}\n'.format(trace['message']))
        for line in trace['stacktrace']:
            if not line['nativeMethod']:  # Filter out native Java methods
                output.write('\tat {}.{}({}:{})\n'.format(line['className'], line['methodName'], line['fileName'], line['lineNumber']))

    result = output.getvalue()
    output.close()
    return result


def add_to_jira(summary, details, stacktrace):
    title = 'HaMIS Exception: ' + summary
    description = '{}\n\nDetails:\n{}\n\nStacktrace:\n{{noformat}}{}{{noformat}}'.format(summary, details, stacktrace)
    issue = {'project': {'key': 'HAMISTIRF'}, 'summary': title, 'description': description,
             'issuetype': {'name': 'Bevinding'}, 'labels': ['Beheer']}
    fields = {'fields': issue}

    log.info('Sending:\n{}'.format(json.dumps(fields)))

    resp = requests.post(_JIRA_URI_CREATE_UPDATE,
                         json=fields,
                         headers=_CONTENT_JSON_HEADER,
                         auth=_JIRA_USER_PASSWD)
    if resp.status_code != 201:
        raise InternalError('Could not create new Jira issue, resp code; {}'.format(resp.status_code))

    return resp.json()


def update_to_jira(issue_id, environment, do_status_transition):
    updated_fields = {'environment': [{'set': environment}]}
    fields = {'update': updated_fields}

    log.info('Sending:\n{}'.format(json.dumps(fields)))
    resp = requests.put(urljoin(_JIRA_URI_CREATE_UPDATE + '/', issue_id),
                        json=fields,
                        headers=_CONTENT_JSON_HEADER,
                        auth=_JIRA_USER_PASSWD)

    if resp.status_code != 204:
        raise InternalError('Could not update existing Jira issue, resp code; {}'.format(resp.status_code))

    if do_status_transition:
        log.info('Update issue status to: {}'.format(_JIRA_TRANSITION_REOPEN_ID))
        resp = requests.post(urljoin(_JIRA_URI_CREATE_UPDATE + '/', issue_id + '/transitions'),
                             json={'transition': {'id': _JIRA_TRANSITION_REOPEN_ID}},
                             headers=_CONTENT_JSON_HEADER,
                             auth=_JIRA_USER_PASSWD)
        log.debug('Transition response: ' + resp.text)
