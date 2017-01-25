"""
Tests for triggering a Jenkins job.
"""
from __future__ import absolute_import
from __future__ import unicode_literals

from itertools import islice
import json
import re
import unittest

import ddt
import requests_mock

from tubular.exception import BackendError
import tubular.jenkins as jenkins

BASE_URL = u'https://test-jenkins'
USER_ID = u'foo'
USER_TOKEN = u'12345678901234567890123456789012'
JOB = u'test-job'
TOKEN = u'asdf'
BUILD_NUM = 456
JOBS_URL = u'{}/job/{}/'.format(BASE_URL, JOB)
JOB_URL = u'{}{}'.format(JOBS_URL, BUILD_NUM)
MOCK_BUILD = {u'number': BUILD_NUM, u'url': JOB_URL}
MOCK_JENKINS_DATA = {'jobs': [{'name': JOB, 'url': JOBS_URL, 'color': 'blue'}]}
MOCK_BUILDS_DATA = {
    'actions': [
        {'parameterDefinitions': [
            {'defaultParameterValue': {'value': '0'}, 'name': 'EXIT_CODE', 'type': 'StringParameterDefinition'}
        ]}
    ],
    'builds': [MOCK_BUILD],
    'lastBuild': MOCK_BUILD
}
MOCK_QUEUE_DATA = {
    'id': 123,
    'task': {'name': JOB, 'url': JOBS_URL},
    'executable': {'number': BUILD_NUM, 'url': JOB_URL}
}
MOCK_BUILD_DATA = {
    'actions': [{}],
    'fullDisplayName': 'foo',
    'number': BUILD_NUM,
    'result': 'SUCCESS',
    'url': JOB_URL,
}


@ddt.ddt
class TestBackoff(unittest.TestCase):
    u"""
    Test of custom backoff code (wait time generator and max_tries)
    """
    @ddt.data(
        (2, 1, 1, 2, [1]),
        (2, 1, 2, 3, [1, 1]),
        (2, 1, 3, 3, [1, 2]),
        (2, 100, 90, 2, [90]),
        (2, 1, 90, 8, [1, 2, 4, 8, 16, 32, 27]),
        (3, 5, 1000, 7, [5, 15, 45, 135, 405, 395]),
    )
    @ddt.unpack
    def test_max_timeout(self, base, factor, timeout, expected_max_tries, expected_waits):
        # pylint: disable=protected-access
        wait_gen, max_tries = jenkins._backoff_timeout(timeout, base, factor)
        self.assertEqual(expected_max_tries, max_tries)

        # Use max_tries-1, because we only wait that many times
        waits = list(islice(wait_gen(), max_tries - 1))
        self.assertEqual(expected_waits, waits)

        self.assertEquals(timeout, sum(waits))


@ddt.ddt
class TestJenkinsAPI(unittest.TestCase):
    """
    Tests for interacting with the Jenkins API
    """
    @requests_mock.Mocker()
    def test_failure(self, mock):
        """
        Test the failure condition when triggering a jenkins job
        """
        # Mock all network interactions
        mock.get(
            re.compile(".*"),
            status_code=404,
        )
        with self.assertRaises(BackendError):
            jenkins.trigger_build(BASE_URL, USER_ID, USER_TOKEN, JOB, TOKEN, None, ())

    @ddt.data(
        (None, ()),
        ('my cause', ()),
        (None, ((u'FOO', u'bar'),)),
        (None, ((u'FOO', u'bar'), (u'BAZ', u'biz'))),
        ('my cause', ((u'FOO', u'bar'),)),
    )
    @ddt.unpack
    @requests_mock.Mocker()
    def test_success(self, cause, param, mock):
        u"""
        Test triggering a jenkins job
        """
        def text_callback(request, context):
            u""" What to return from the mock. """
            # This is the initial call that jenkinsapi uses to
            # establish connectivity to Jenkins
            # https://test-jenkins/api/python?tree=jobs[name,color,url]
            context.status_code = 200
            if request.url.startswith(u'https://test-jenkins/api/python'):
                return json.dumps(MOCK_JENKINS_DATA)
            elif request.url.startswith(u'https://test-jenkins/job/test-job/456'):
                return json.dumps(MOCK_BUILD_DATA)
            elif request.url.startswith(u'https://test-jenkins/job/test-job'):
                return json.dumps(MOCK_BUILDS_DATA)
            elif request.url.startswith(u'https://test-jenkins/queue/item/123/api/python'):
                return json.dumps(MOCK_QUEUE_DATA)
            else:
                # We should never get here, unless the jenkinsapi implementation changes.
                # This response will catch that condition.
                context.status_code = 500
                return None

        # Mock all network interactions
        mock.get(
            re.compile('.*'),
            text=text_callback
        )
        mock.post(
            '{}/job/test-job/buildWithParameters'.format(BASE_URL),
            status_code=201,  # Jenkins responds with a 201 Created on success
            headers={'location': '{}/queue/item/123'.format(BASE_URL)}
        )

        # Make the call to the Jenkins API
        result = jenkins.trigger_build(BASE_URL, USER_ID, USER_TOKEN, JOB, TOKEN, cause, param)
        self.assertEqual(result, 'SUCCESS')
