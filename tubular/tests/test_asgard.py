"""
Tests of the code interacting with the Asgard API.
"""
from __future__ import unicode_literals

import os
import json
import unittest
import itertools
import boto
import mock
import httpretty

from ddt import ddt, data, unpack
from moto import mock_ec2, mock_autoscaling, mock_elb
from moto.ec2.utils import random_ami_id
from requests.exceptions import ConnectionError
import tubular.asgard as asgard
from tubular.exception import (
    TimeoutException,
    BackendError,
    CannotDeleteActiveASG,
    CannotDeleteLastASG,
    ASGDoesNotExistException
)
from tubular.tests.test_utils import create_asg_with_tags, create_elb
from tubular.ec2 import tag_asg_for_deletion

# Disable the retry decorator and reload the asgard module. This will ensure that tests do not fail because of the retry
# decorator recalling a method when using httpretty with side effect iterators
os.environ['TUBULAR_RETRY_ENABLED'] = "false"
reload(asgard)


SAMPLE_CLUSTER_LIST = """
[
  {
    "cluster": "loadtest-edx-edxapp",
    "autoScalingGroups":
    [
      "loadtest-edx-edxapp-v058",
      "loadtest-edx-edxapp-v059"
    ]
  },
  {
    "cluster": "loadtest-edx-insights",
    "autoScalingGroups":
    [
      "loadtest-edx-insights-v002"
    ]
  },
  {
    "cluster": "loadtest-edx-worker",
    "autoScalingGroups":
    [
      "loadtest-edx-worker-v034"
    ]
  }
]"""

BAD_CLUSTER_JSON1 = """
<HTML><HEAD>Have some HTML</HEAD></HTML>
"""

BAD_CLUSTER_JSON2 = """
[
  {
    "autoScalingGroups":
    [
      "loadtest-edx-edxapp-v058",
      "loadtest-edx-edxapp-v059"
    ]
  }
]"""

HTML_RESPONSE_BODY = "<html>This is not JSON...</html>"

VALID_CLUSTER_JSON_INFO = """
[
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v058",
    "availabilityZones":
    [
      "us-east-1b",
      "us-east-1c"
    ],
    "createdTime": "2016-02-10T12:23:10Z",
    "defaultCooldown": 300,
    "desiredCapacity": 4,
    "minSize": 4
  },
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v059",
    "availabilityZones":
    [
      "us-east-1b",
      "us-east-1c"
    ],
    "createdTime": "2016-02-10T12:23:10Z",
    "defaultCooldown": 300,
    "desiredCapacity": 4,
    "minSize": 4
  }
]
"""

VALID_SINGLE_ASG_CLUSTER_INFO_JSON = """
[
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v060",
    "availabilityZones":
    [
      "us-east-1b",
      "us-east-1c"
    ],
    "createdTime": "2016-02-10T12:23:10Z",
    "defaultCooldown": 300,
    "desiredCapacity": 4
  }
]


"""

ASGS_FOR_EDXAPP_BEFORE = """
[
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v058",
    "desiredCapacity": 4,
    "minSize": 4,
    "maxSize": 4
  },
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v059",
    "desiredCapacity": 4,
    "minSize": 4,
    "maxSize": 4
  }
]
"""

ASGS_FOR_EDXAPP_AFTER = """
[
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v058",
    "desiredCapacity": 0,
    "minSize": 0,
    "maxSize": 0
  },
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v059",
    "desiredCapacity": 4,
    "minSize": 4,
    "maxSize": 4
  },
  {
    "autoScalingGroupName": "loadtest-edx-edxapp-v099",
    "desiredCapacity": 4,
    "minSize": 4,
    "maxSize": 4
  }
]
"""

ASGS_FOR_WORKER_BEFORE = """
[
  {
    "autoScalingGroupName": "loadtest-edx-worker-v034",
    maxSize: 1,
    minSize: 1,
    desiredCapacity: 1
  }
]
"""
ASGS_FOR_WORKER_AFTER = """
[
  {
    "autoScalingGroupName": "loadtest-edx-worker-v034",
    "desiredCapacity": 0,
    "minSize": 0,
    "maxSize": 0
  },
  {
    "autoScalingGroupName": "loadtest-edx-worker-v099",
    "desiredCapacity": 4,
    "minSize": 4,
    "maxSize": 4
  }
]
"""


DELETED_ASG_IN_PROGRESS = """
{{
    "group": {{
        "autoScalingGroupName": "{0}",
        "loadBalancerNames":
        [
            "app_elb"
        ],
        "status": "deleted",
        "launchingSuspended": false,
        "desiredCapacity": 4,
        "minSize": 4
    }},
    "clusterName": "app_cluster"
}}
"""

DELETED_ASG_NOT_IN_PROGRESS = """
{{
    "group": {{
        "autoScalingGroupName": "{0}",
        "loadBalancerNames": [
            "app_elb"
        ],
        "status": null,
        "desiredCapacity": 4,
        "minSize": 4,
        "launchingSuspended": true
    }},
    "clusterName": "app_cluster"
}}
"""

DISABLED_ASG = """
{{
    "group": {{
        "autoScalingGroupName": "{0}",
        "loadBalancerNames": [
            "app_elb"
        ],
        "desiredCapacity": 0,
        "minSize": 0,
        "status": null,
        "launchingSuspended": true
    }},
    "clusterName": "app_cluster"
}}
"""

ENABLED_ASG = """
{{
    "group": {{
        "autoScalingGroupName": "{0}",
        "loadBalancerNames": [
            "app_elb"
        ],
        "desiredCapacity": 4,
        "minSize": 4,
        "status": null,
        "launchingSuspended": false
    }},
    "clusterName": "app_cluster"
}}
"""


FAILED_SAMPLE_TASK = """
{
  "log":
  [
    "2016-02-11_02:31:18 Started on thread Task:Force Delete Auto Scaling Group 'loadtest-edx-edxapp-v060'.",
    "2016-02-11_02:31:18 Deregistering all instances in 'loadtest-edx-edxapp-v060' from load balancers",
    "2016-02-11_02:31:18 Deregister all instances in Auto Scaling Group 'loadtest-edx-edxapp-v060' from ELBs",
    "2016-02-11_02:31:19 Deleting auto scaling group 'loadtest-edx-edxapp-v060'",
    "2016-02-11_02:31:19 Delete Auto Scaling Group 'loadtest-edx-edxapp-v060'",
    "2016-02-11_02:31:19 Auto scaling group 'loadtest-edx-edxapp-v060' will be deleted after deflation finishes",
    "2016-02-11_02:41:24 Exception: com.netflix.asgard.push.PushException: Timeout waiting 10m for auto scaling group 'loadtest-edx-edxapp-v060' to disappear from AWS."
  ],
  "status": "failed",
  "operation": "",
  "durationString": "10m 6s",
  "updateTime": "2016-02-11 02:41:24 UTC"
}
"""

COMPLETED_SAMPLE_TASK = """
{
  "log":
  [
    "2016-02-11_02:31:11 Started on thread Task:Stopping traffic to instances of loadtest-edx-edxapp-v060.",
    "2016-02-11_02:31:11 Disabling new instance launching for auto scaling group 'loadtest-edx-edxapp-v060'",
    "2016-02-11_02:31:12 Disabling instance termination for auto scaling group 'loadtest-edx-edxapp-v060'",
    "2016-02-11_02:31:12 Disabling adding instances to ELB for auto scaling group 'loadtest-edx-edxapp-v060'",
    "2016-02-11_02:31:12 Completed in 0s."
  ],
  "status": "completed",
  "operation": "",
  "durationString": "0s",
  "updateTime": "2016-02-11 02:31:12 UTC"
}
"""

RUNNING_SAMPLE_TASK = """
{
  "log":
  [
    "2016-02-11_19:03:34 Started on thread Task:Creating auto scaling group 'loadtest-edx-edxapp-v059', min 4, max 4, traffic prevented.",
    "2016-02-11_19:03:34 Group 'loadtest-edx-edxapp-v059' will start with 0 instances",
    "2016-02-11_19:03:34 Create Auto Scaling Group 'loadtest-edx-edxapp-v059'",
    "2016-02-11_19:03:34 Create Launch Configuration 'loadtest-edx-edxapp-v059-20160211190334' with image 'ami-f2032998'",
    "2016-02-11_19:03:35 Create Autoscaling Group 'loadtest-edx-edxapp-v059'",
    "2016-02-11_19:03:35 Disabling adding instances to ELB for auto scaling group 'loadtest-edx-edxapp-v059'",
    "2016-02-11_19:03:35 Launch Config 'loadtest-edx-edxapp-v059-20160211190334' has been created. Auto Scaling Group 'loadtest-edx-edxapp-v059' has been created. ",
    "2016-02-11_19:03:35 Create 1 LifecycleHook",
    "2016-02-11_19:03:35 Create LifecycleHook with loadtest-edx-GetTrackingLogs",
    "2016-02-11_19:03:36 Resizing group 'loadtest-edx-edxapp-v059' to min 4, max 4",
    "2016-02-11_19:03:36 Setting group 'loadtest-edx-edxapp-v059' to min 4 max 4",
    "2016-02-11_19:03:36 Update Autoscaling Group 'loadtest-edx-edxapp-v059'",
    "2016-02-11_19:03:37 Group 'loadtest-edx-edxapp-v059' has 0 instances. Waiting for 4 to exist."
  ],
  "status": "running",
  "operation": "Group 'loadtest-edx-edxapp-v059' has 0 instances. Waiting for 4 to exist.",
  "durationString": "16s",
  "updateTime": "2016-02-11 19:03:37 UTC"
}
"""

SAMPLE_ASG_INFO = """
{
  "group": {
    "loadBalancerNames":
    [
      "app_elb"
    ],
    "desiredCapacity": 4,
    "minSize": 4
  }
}
"""

SAMPLE_WORKER_ASG_INFO = """
{
  "group": {
    "loadBalancerNames": [],
    "desiredCapacity": 4,
    "minSize": 4
  }
}
"""


@ddt
class TestAsgard(unittest.TestCase):
    """
    Class containing all Asgard tests.
    """
    _multiprocess_can_split_ = True

    def test_bad_clusters_endpoint(self):
        relevant_asgs = []
        self.assertRaises(ConnectionError, asgard.clusters_for_asgs, relevant_asgs)

    @httpretty.activate
    def test_clusters_for_asgs(self):
        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_LIST_URL,
            body=SAMPLE_CLUSTER_LIST,
            content_type="application/json")

        relevant_asgs = []
        cluster_names = asgard.clusters_for_asgs(relevant_asgs)
        self.assertEqual({}, cluster_names)

        relevant_asgs = ["loadtest-edx-edxapp-v058"]
        expected_clusters = {
            "loadtest-edx-edxapp": ["loadtest-edx-edxapp-v058", "loadtest-edx-edxapp-v059"]
        }
        cluster_names = asgard.clusters_for_asgs(relevant_asgs)
        self.assertEqual(expected_clusters, cluster_names)

        relevant_asgs = ["loadtest-edx-edxapp-v058", "loadtest-edx-worker-v034"]
        cluster_names = asgard.clusters_for_asgs(relevant_asgs)
        self.assertIn("loadtest-edx-edxapp", cluster_names)
        self.assertIn("loadtest-edx-worker", cluster_names)
        self.assertEqual(["loadtest-edx-worker-v034"], cluster_names['loadtest-edx-worker'])
        self.assertEqual(
            ["loadtest-edx-edxapp-v058", "loadtest-edx-edxapp-v059"],
            cluster_names['loadtest-edx-edxapp']
        )

    @httpretty.activate
    def test_clusters_for_asgs_bad_response(self):
        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_LIST_URL,
            body=HTML_RESPONSE_BODY,
            content_type="text/html")

        relevant_asgs = []
        with self.assertRaises(BackendError):
            __ = asgard.clusters_for_asgs(relevant_asgs)

    @data(BAD_CLUSTER_JSON1, BAD_CLUSTER_JSON2)
    @httpretty.activate
    def test_incorrect_json(self, response_json):
        # The json is valid but not the structure we expected.
        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_LIST_URL,
            body=response_json,
            content_type="application/json")

        relevant_asgs = []
        self.assertRaises(BackendError, asgard.clusters_for_asgs, relevant_asgs)

    @httpretty.activate
    def test_asg_for_cluster(self):
        cluster = "prod-edx-edxapp"
        url = asgard.CLUSTER_INFO_URL.format(cluster)
        httpretty.register_uri(
            httpretty.GET,
            url,
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json")

        expected_asgs = ["loadtest-edx-edxapp-v058", "loadtest-edx-edxapp-v059"]
        returned_asgs = [asg['autoScalingGroupName'] for asg in asgard.asgs_for_cluster(cluster)]
        self.assertEqual(expected_asgs, returned_asgs)

    @httpretty.activate
    def test_asg_for_cluster_bad_response(self):
        cluster = "prod-edx-edxapp"
        url = asgard.CLUSTER_INFO_URL.format(cluster)
        httpretty.register_uri(
            httpretty.GET,
            url,
            body=HTML_RESPONSE_BODY,
            content_type="text/html")

        expected_asgs = ["loadtest-edx-edxapp-v058", "loadtest-edx-edxapp-v059"]
        with self.assertRaises(BackendError):
            self.assertEqual(expected_asgs, asgard.asgs_for_cluster(cluster))

    def test_bad_asgs_endpoint(self):
        cluster = "Fake cluster"
        self.assertRaises(ConnectionError, asgard.asgs_for_cluster, cluster)

    @httpretty.activate
    def test_asg_for_cluster_incorrect_json(self):
        cluster = "prod-edx-edxapp"
        url = asgard.CLUSTER_INFO_URL.format(cluster)
        httpretty.register_uri(
            httpretty.GET,
            url,
            body=BAD_CLUSTER_JSON1,
            content_type="application/json")

        self.assertRaises(BackendError, asgard.asgs_for_cluster, cluster)

    @httpretty.activate
    def test_elbs_for_asg(self):
        asg_info_url = asgard.ASG_INFO_URL.format("test_asg")
        httpretty.register_uri(
            httpretty.GET,
            asg_info_url,
            body=SAMPLE_ASG_INFO,
            content_type="application/json")

        self.assertEqual(asgard.elbs_for_asg("test_asg"), ["app_elb"])

    @httpretty.activate
    def test_elbs_for_asg_bad_data(self):
        asg_info_url = asgard.ASG_INFO_URL.format("test_asg")
        httpretty.register_uri(
            httpretty.GET,
            asg_info_url,
            body=HTML_RESPONSE_BODY,
            content_type="text/html")

        self.assertRaises(BackendError, asgard.elbs_for_asg, "test_asg")

    @httpretty.activate
    def test_elbs_for_asg_bad_response(self):
        asg_info_url = asgard.ASG_INFO_URL.format("test_asg")
        httpretty.register_uri(
            httpretty.GET,
            asg_info_url,
            body=BAD_CLUSTER_JSON1,
            content_type="application/json")

        self.assertRaises(BackendError, asgard.elbs_for_asg, "test_asg")

    def test_bad_asg_info_endpoint(self):
        self.assertRaises(ConnectionError, asgard.elbs_for_asg, "fake_asg")

    @httpretty.activate
    def test_task_completion(self):
        task_url = "http://some.host/task/1234.json"
        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=COMPLETED_SAMPLE_TASK,
            content_type="application/json")

        actual_output = asgard.wait_for_task_completion(task_url, 1)
        expected_output = json.loads(COMPLETED_SAMPLE_TASK)
        self.assertEqual(expected_output, actual_output)

    @httpretty.activate
    def test_failed_task_completion(self):
        task_url = "http://some.host/task/1234.json"
        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=FAILED_SAMPLE_TASK,
            content_type="application/json")

        actual_output = asgard.wait_for_task_completion(task_url, 1)
        expected_output = json.loads(FAILED_SAMPLE_TASK)
        self.assertEqual(expected_output, actual_output)

    @httpretty.activate
    def test_running_then_failed_task_completion(self):
        task_url = "http://some.host/task/1234.json"
        httpretty.register_uri(
            httpretty.GET,
            task_url,
            responses=[
                httpretty.Response(body=RUNNING_SAMPLE_TASK),
                httpretty.Response(FAILED_SAMPLE_TASK),
            ],
            content_type="application/json")

        with mock.patch('tubular.asgard.WAIT_SLEEP_TIME', 1):
            actual_output = asgard.wait_for_task_completion(task_url, 2)
            expected_output = json.loads(FAILED_SAMPLE_TASK)
            self.assertEqual(expected_output, actual_output)

    @httpretty.activate
    def test_task_completion_bad_response(self):
        task_url = "http://some.host/task/1234.json"
        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=HTML_RESPONSE_BODY,
            content_type="text/html")

        with self.assertRaises(BackendError):
            asgard.wait_for_task_completion(task_url, 1)

    @httpretty.activate
    def test_task_timeout(self):
        task_url = "http://some.host/task/1234.json"
        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=RUNNING_SAMPLE_TASK,
            content_type="application/json")

        self.assertRaises(TimeoutException, asgard.wait_for_task_completion, task_url, 1)

    @httpretty.activate
    def test_new_asg(self):
        task_url = "http://some.host/task/1234.json"
        cluster = "loadtest-edx-edxapp"
        ami_id = "ami-abc1234"

        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            self.assertEqual('POST', request.method)
            expected_request_body = {"name": [cluster], "imageId": [ami_id]}
            expected_querystring = {"asgardApiToken": ['dummy-token']}

            self.assertEqual(expected_request_body, request.parsed_body)
            self.assertEqual(expected_querystring, request.querystring)
            response_headers = {
                "Location": task_url,
                "server": asgard.ASGARD_API_ENDPOINT
            }
            response_body = ""
            return (302, response_headers, response_body)

        httpretty.register_uri(
            httpretty.POST,
            asgard.NEW_ASG_URL,
            body=post_callback,
            Location=task_url)

        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=COMPLETED_SAMPLE_TASK,
            content_type="application/json")

        url = asgard.CLUSTER_INFO_URL.format(cluster)
        httpretty.register_uri(
            httpretty.GET,
            url,
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json")

        expected_asg = "loadtest-edx-edxapp-v059"
        self.assertEqual(expected_asg, asgard.new_asg(cluster, ami_id))

    @httpretty.activate
    @data(
        (  # task to deploy a cluster failed.
            "http://some.host/task/1234.json",
            302,
            {"Location": "http://some.host/task/1234", "server": asgard.ASGARD_API_ENDPOINT},
            "",
            FAILED_SAMPLE_TASK,
            200,
            VALID_CLUSTER_JSON_INFO,
            BackendError
        ),
        (  # Cluster not found after creation
            "http://some.host/task/1234.json",
            302,
            {"Location": "http://some.host/task/1234", "server": asgard.ASGARD_API_ENDPOINT},
            "",
            FAILED_SAMPLE_TASK,
            404,
            VALID_CLUSTER_JSON_INFO,
            BackendError
        ),
        (  # Task creation failed
            "http://some.host/task/1234.json",
            500,
            {"Location": "http://some.host/task/1234", "server": asgard.ASGARD_API_ENDPOINT},
            "",
            FAILED_SAMPLE_TASK,
            200,
            VALID_CLUSTER_JSON_INFO,
            BackendError
        ),
        (  # failed to create ASG
            "http://some.host/task/1234.json",
            404,
            {"Location": "http://some.host/task/1234", "server": asgard.ASGARD_API_ENDPOINT},
            "",
            FAILED_SAMPLE_TASK,
            200,
            VALID_CLUSTER_JSON_INFO,
            BackendError
        ),
    )
    @unpack
    def test_new_asg_failure(self,
                             task_url,
                             create_response_code,
                             create_response_headers,
                             create_response_body,
                             task_response_body,
                             cluster_response_code,
                             cluster_response_body,
                             expected_exception):
        cluster = "loadtest-edx-edxapp"
        ami_id = "ami-abc1234"

        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            self.assertEqual('POST', request.method)
            expected_request_body = {"name": [cluster], "imageId": [ami_id]}
            expected_querystring = {"asgardApiToken": ['dummy-token']}

            self.assertEqual(expected_request_body, request.parsed_body)
            self.assertEqual(expected_querystring, request.querystring)
            return create_response_code, create_response_headers, create_response_body

        httpretty.register_uri(
            httpretty.POST,
            asgard.NEW_ASG_URL,
            body=post_callback,
            Location=task_url)

        # Mock 'Task' response
        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=task_response_body,
            content_type="application/json")

        # Mock 'Cluster' response
        url = asgard.CLUSTER_INFO_URL.format(cluster)
        httpretty.register_uri(
            httpretty.GET,
            url,
            status=cluster_response_code,
            body=cluster_response_body,
            content_type="application/json")

        self.assertRaises(expected_exception, asgard.new_asg, cluster, ami_id)

    @httpretty.activate
    def test_new_asg_404(self):
        cluster = "loadtest-edx-edxapp"
        ami_id = "ami-abc1234"

        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            response_headers = {"server": asgard.ASGARD_API_ENDPOINT}
            return (404, response_headers, "")

        httpretty.register_uri(
            httpretty.POST,
            asgard.NEW_ASG_URL,
            body=post_callback,
        )

        self.assertRaises(BackendError, asgard.new_asg, cluster, ami_id)

    @httpretty.activate
    @mock.patch('boto.connect_autoscale')
    def test_disable_asg_pending_deletion(self, mock_connect_autoscale):  # pylint: disable=unused-argument
        """
        Tests an ASG disable that is cancelled due to the ASG pending deletion.
        """
        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            # If this POST callback gets called, test has failed.
            raise Exception("POST called to disable ASG when it should have been skipped.")

        httpretty.register_uri(
            httpretty.POST,
            asgard.ASG_DEACTIVATE_URL,
            body=post_callback
        )

        # setup the mocking of the is asg pending delete calls
        asg = 'loadtest-edx-edxapp-v059'
        self._mock_asgard_pending_delete([asg])

        self.assertEquals(None, asgard.disable_asg(asg))

    @httpretty.activate
    @mock.patch('boto.connect_autoscale')
    def test_disable_asg_does_not_exist(self, mock_connect_autoscale):  # pylint: disable=unused-argument
        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            # If this POST callback gets called, test has failed.
            raise Exception("POST called to disable ASG when it should have been skipped.")

        httpretty.register_uri(
            httpretty.POST,
            asgard.ASG_DEACTIVATE_URL,
            body=post_callback
        )

        asg = 'loadtest-edx-edxapp-v059'
        self._mock_asgard_pending_delete([asg], 404)
        self.assertEquals(None, asgard.disable_asg(asg))

    @httpretty.activate
    @data((COMPLETED_SAMPLE_TASK, True), (FAILED_SAMPLE_TASK, False))
    @unpack
    def test_delete_asg(self, task_body, should_succeed):
        asg = "loadtest-edx-edxapp-v060"
        cluster = "app_cluster"
        self._mock_asgard_not_pending_delete([asg])

        task_url = "http://some.host/task/1234.json"

        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            self.assertEqual('POST', request.method)
            expected_request_body = {"name": [asg]}
            expected_querystring = {"asgardApiToken": ['dummy-token']}

            self.assertEqual(expected_request_body, request.parsed_body)
            self.assertEqual(expected_querystring, request.querystring)
            response_headers = {"Location": task_url.strip(".json"),
                                "server": asgard.ASGARD_API_ENDPOINT}
            response_body = ""
            return (302, response_headers, response_body)

        httpretty.register_uri(
            httpretty.POST,
            asgard.ASG_DELETE_URL,
            body=post_callback
        )

        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=task_body,
            content_type="application/json"
        )

        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_INFO_URL.format(cluster),
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json"
        )

        if should_succeed:
            self.assertEqual(None, asgard.delete_asg(asg, False))
        else:
            self.assertRaises(BackendError, asgard.delete_asg, asg, False)

    @httpretty.activate
    def test_delete_asg_active(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_not_pending_delete([asg], body=ENABLED_ASG)

        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            raise Exception("This post should not be called")

        httpretty.register_uri(
            httpretty.POST,
            asgard.ASG_DELETE_URL,
            body=post_callback
        )
        self.assertRaises(CannotDeleteActiveASG, asgard.delete_asg, asg, True)

    @httpretty.activate
    def test_delete_asg_pending_delete(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_pending_delete([asg])
        self.assertEqual(None, asgard.delete_asg(asg, True))

    @httpretty.activate
    def test_delete_last_asg(self):
        asg = "loadtest-edx-edxapp-v060"
        cluster = "app_cluster"
        self._mock_asgard_not_pending_delete([asg], body=DISABLED_ASG)

        httpretty.register_uri(
            httpretty.GET,
            asgard.ASG_INFO_URL.format(asg),
            body=DISABLED_ASG.format(asg),
            content_type="application/json"
        )

        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_INFO_URL.format(cluster),
            body=VALID_SINGLE_ASG_CLUSTER_INFO_JSON,
            content_type="application/json"
        )

        self.assertRaises(CannotDeleteLastASG, asgard.delete_asg, asg)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @data(*itertools.product(
        ((asgard.ASG_ACTIVATE_URL, asgard.enable_asg), (asgard.ASG_DEACTIVATE_URL, asgard.disable_asg)),
        (True, False)
    ))
    @unpack
    def test_enable_disable_asg(self, url_and_function, success):
        """
        Tests enabling and disabling ASGs, with both success and failure.
        """
        endpoint_url, test_function = url_and_function
        task_url = "http://some.host/task/1234.json"
        asg = "loadtest-edx-edxapp-v059"
        cluster = "app_cluster"

        def post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            self.assertEqual('POST', request.method)
            expected_request_body = {"name": [asg]}
            expected_querystring = {"asgardApiToken": ['dummy-token']}

            self.assertEqual(expected_request_body, request.parsed_body)
            self.assertEqual(expected_querystring, request.querystring)
            response_headers = {
                "Location": task_url.strip(".json"),
                "server": asgard.ASGARD_API_ENDPOINT
            }
            response_body = ""
            return (302, response_headers, response_body)

        httpretty.register_uri(
            httpretty.POST,
            endpoint_url,
            body=post_callback
        )

        httpretty.register_uri(
            httpretty.GET,
            asgard.ASG_INFO_URL.format(asg),
            body=ENABLED_ASG.format(asg),
            content_type="application/json"
        )

        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_INFO_URL.format(cluster),
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json"
        )

        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=COMPLETED_SAMPLE_TASK if success else FAILED_SAMPLE_TASK,
            content_type="application/json"
        )

        url = asgard.ASG_INFO_URL.format(asg)
        httpretty.register_uri(
            httpretty.GET,
            url,
            body=DELETED_ASG_NOT_IN_PROGRESS.format(asg),
            content_type="application/json")

        if success:
            self.assertEquals(None, test_function(asg))
        else:
            self.assertRaises(BackendError, test_function, asg)

    def _setup_for_deploy(
            self,
            new_asg_task_status=COMPLETED_SAMPLE_TASK,
            enable_asg_task_status=COMPLETED_SAMPLE_TASK,
            disable_asg_task_status=COMPLETED_SAMPLE_TASK,
    ):
        """
        Setup all the variables for an ASG deployment.
        """
        # Make the AMI
        ec2 = boto.connect_ec2()
        reservation = ec2.run_instances(random_ami_id())
        instance_id = reservation.instances[0].id
        ami_id = ec2.create_image(instance_id, "Existing AMI")
        ami = ec2.get_all_images(ami_id)[0]
        ami.add_tag("environment", "foo")
        ami.add_tag("deployment", "bar")
        ami.add_tag("play", "baz")

        # Make the current ASGs
        # pylint: disable=attribute-defined-outside-init
        self.test_asg_tags = {
            "environment": "foo",
            "deployment": "bar",
            "play": "baz",
        }

        self.test_elb_name = "app_elb"
        create_elb(self.test_elb_name)

        create_asg_with_tags("loadtest-edx-edxapp-v058", self.test_asg_tags, ami_id, [self.test_elb_name])
        create_asg_with_tags("loadtest-edx-edxapp-v059", self.test_asg_tags, ami_id, [self.test_elb_name])
        create_asg_with_tags("loadtest-edx-worker-v034", self.test_asg_tags, ami_id, [])

        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_LIST_URL,
            body=SAMPLE_CLUSTER_LIST,
            content_type="application/json")

        edxapp_cluster_info_url = asgard.CLUSTER_INFO_URL.format("loadtest-edx-edxapp")
        httpretty.register_uri(
            httpretty.GET,
            edxapp_cluster_info_url,
            responses=[
                # httpretty.Response(body=ASGS_FOR_EDXAPP_BEFORE),
                httpretty.Response(body=ASGS_FOR_EDXAPP_AFTER),
            ],
        )

        worker_cluster_info_url = asgard.CLUSTER_INFO_URL.format("loadtest-edx-worker")
        httpretty.register_uri(
            httpretty.GET,
            worker_cluster_info_url,
            responses=[
                # httpretty.Response(body=ASGS_FOR_WORKER_BEFORE),
                httpretty.Response(body=ASGS_FOR_WORKER_AFTER),
            ],
        )

        # Mock endpoints for building new ASGs
        task_url = "http://some.host/task/new_asg_1234.json"

        def new_asg_post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            response_headers = {
                "Location": task_url,
                "server": asgard.ASGARD_API_ENDPOINT
            }
            response_body = ""
            new_asg_name = "{}-v099".format(request.parsed_body["name"][0])
            new_ami_id = request.parsed_body["imageId"][0]
            create_asg_with_tags(new_asg_name, self.test_asg_tags, new_ami_id)
            return (302, response_headers, response_body)

        httpretty.register_uri(
            httpretty.POST,
            asgard.NEW_ASG_URL,
            body=new_asg_post_callback,
            Location=task_url)

        httpretty.register_uri(
            httpretty.GET,
            task_url,
            body=new_asg_task_status,
            content_type="application/json")

        # Make endpoint for enabling new ASGs
        enable_asg_task_url = "http://some.host/task/enable_asg_1234.json"

        def enable_asg_post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            response_headers = {
                "Location": enable_asg_task_url,
                "server": asgard.ASGARD_API_ENDPOINT
            }
            response_body = ""
            return (302, response_headers, response_body)

        disable_asg_task_url = "http://some.host/task/disable_asg_1234.json"

        def disable_asg_post_callback(request, uri, headers):  # pylint: disable=unused-argument
            """
            Callback method for POST.
            """
            response_headers = {
                "Location": disable_asg_task_url,
                "server": asgard.ASGARD_API_ENDPOINT
            }
            response_body = ""
            return (302, response_headers, response_body)

        httpretty.register_uri(
            httpretty.POST,
            asgard.ASG_ACTIVATE_URL,
            body=enable_asg_post_callback)

        httpretty.register_uri(
            httpretty.POST,
            asgard.ASG_DEACTIVATE_URL,
            body=disable_asg_post_callback)

        httpretty.register_uri(
            httpretty.GET,
            disable_asg_task_url,
            body=disable_asg_task_status,
            content_type="application/json")

        httpretty.register_uri(
            httpretty.GET,
            enable_asg_task_url,
            body=enable_asg_task_status,
            content_type="application/json")

        return ami_id

    def _mock_asgard_not_pending_delete(
            self, asgs, response_code=200, body=DELETED_ASG_NOT_IN_PROGRESS, html_return=False
    ):
        """
        This helper function will mock calls to the asgard api related to is_asg_pending_delete. The response will be
        that this ASG is not pending delete.

        Arguments:
            asgs(list<str>): a list of the ASG names that are being checked
            response_code(int): an HTML response code sent from Asgard
            body(str): Format string for JSON response
            html_return(boolean): If True, return HTML instead of JSON

        Returns:
            None
        """
        for asg in asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            response_content_type = "application/json"
            response_body = body.format(asg)
            if html_return:
                response_content_type = "text/html"
                response_body = HTML_RESPONSE_BODY
            httpretty.register_uri(
                httpretty.GET,
                url,
                body=response_body,
                content_type=response_content_type,
                status=response_code)

    def _mock_asgard_pending_delete(self, asgs, response_code=200):
        """
        This helper function will mock calls to the asgard api related to is_asg_pending_delete.  The response will be
        that this ASG is pending delete.

        Arguments:
            asgs(list<str>): a list of the ASG names that are being checked
            response_code(int): an HTML response code sent from Asgard

        Returns:
            None
        """
        for asg in asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            httpretty.register_uri(
                httpretty.GET,
                url,
                body=DELETED_ASG_IN_PROGRESS.format(asg),
                content_type="application/json",
                status=response_code)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_deploy_asg_failed(self):
        ami_id = self._setup_for_deploy(
            new_asg_task_status=FAILED_SAMPLE_TASK
        )
        self.assertRaises(Exception, asgard.deploy, ami_id)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_deploy_enable_asg_failed(self):
        ami_id = self._setup_for_deploy(
            new_asg_task_status=COMPLETED_SAMPLE_TASK,
            enable_asg_task_status=FAILED_SAMPLE_TASK
        )
        self.assertRaises(Exception, asgard.deploy, ami_id)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_deploy_elb_health_failed(self):
        ami_id = self._setup_for_deploy(COMPLETED_SAMPLE_TASK, COMPLETED_SAMPLE_TASK)
        mock_function = "tubular.ec2.wait_for_healthy_elbs"
        with mock.patch(mock_function, side_effect=Exception("Never became healthy.")):
            self.assertRaises(Exception, asgard.deploy, ami_id)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_deploy(self):
        ami_id = self._setup_for_deploy()

        not_in_service_asgs = ["loadtest-edx-edxapp-v058"]
        in_service_asgs = ["loadtest-edx-edxapp-v059", "loadtest-edx-worker-v034"]
        new_asgs = ["loadtest-edx-edxapp-v099", "loadtest-edx-worker-v099"]

        self._mock_asgard_not_pending_delete(in_service_asgs, body=ENABLED_ASG)
        self._mock_asgard_pending_delete(not_in_service_asgs)

        cluster = "app_cluster"
        for asg in new_asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            httpretty.register_uri(
                httpretty.GET,
                url,
                responses=[
                    httpretty.Response(body=DELETED_ASG_NOT_IN_PROGRESS.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=DELETED_ASG_NOT_IN_PROGRESS.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=ENABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                ])

        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_INFO_URL.format(cluster),
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json"
        )
        expected_output = {
            'ami_id': ami_id,
            'current_asgs':
                {
                    'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v099'],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v099']
                },
            'disabled_asgs':
                {
                    'loadtest-edx-edxapp':
                        [
                            'loadtest-edx-edxapp-v058',
                            'loadtest-edx-edxapp-v059'
                        ],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v034']
                }
        }

        self.assertEqual(cmp(expected_output, asgard.deploy(ami_id)), 0)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_deploy_new_asg_disabled(self):
        ami_id = self._setup_for_deploy()
        asgs = ["loadtest-edx-edxapp-v058", "loadtest-edx-edxapp-v059",
                "loadtest-edx-edxapp-v099", "loadtest-edx-worker-v099"]
        for asg in asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            httpretty.register_uri(
                httpretty.GET,
                url,
                responses=[
                    httpretty.Response(body=DELETED_ASG_NOT_IN_PROGRESS.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=DELETED_ASG_IN_PROGRESS.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=DISABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200)
                ]
            )
        self.assertRaises(BackendError, asgard.deploy, ami_id)

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_rollback(self):
        ami_id = self._setup_for_deploy()

        in_service_pre_rollback_asgs = [
            "loadtest-edx-edxapp-v099", "loadtest-edx-worker-v099"
        ]
        in_service_post_rollback_asgs = [
            "loadtest-edx-edxapp-v058", "loadtest-edx-edxapp-v059", "loadtest-edx-worker-v034"
        ]

        for asg in in_service_pre_rollback_asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            httpretty.register_uri(
                httpretty.GET,
                url,
                responses=[
                    # Start enabled and finish disabled.
                    httpretty.Response(body=ENABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=ENABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=DISABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                ]
            )
        for asg in in_service_post_rollback_asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            httpretty.register_uri(
                httpretty.GET,
                url,
                responses=[
                    # Start disabled and finish enabled.
                    httpretty.Response(body=DISABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=DISABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=ENABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                ]
            )
        cluster = "app_cluster"
        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_INFO_URL.format(cluster),
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json"
        )

        rollback_input = {
            'current_asgs': {
                'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v099'],
                'loadtest-edx-worker': ['loadtest-edx-worker-v099']
            },
            'disabled_asgs': {
                'loadtest-edx-edxapp':
                    [
                        'loadtest-edx-edxapp-v058',
                        'loadtest-edx-edxapp-v059'
                    ],
                'loadtest-edx-worker': ['loadtest-edx-worker-v034']
            },
        }
        # The expected output is the rollback input with reversed current/disabled asgs.
        expected_output = {}
        expected_output['current_asgs'] = rollback_input['disabled_asgs']
        expected_output['disabled_asgs'] = rollback_input['current_asgs']
        expected_output['ami_id'] = ami_id

        # Rollback and check output.
        self.assertEqual(
            asgard.rollback(rollback_input['current_asgs'], rollback_input['disabled_asgs'], ami_id),
            expected_output
        )

    def _setup_rollback(self):
        """
        Setup the scenario where an ASG deployment is rolled-back to a previous ASG.
        """
        # pylint: disable=attribute-defined-outside-init
        self.test_ami_id = self._setup_for_deploy()

        not_in_service_asgs = ["loadtest-edx-edxapp-v058"]
        in_service_pre_rollback_asgs = ["loadtest-edx-edxapp-v059", "loadtest-edx-worker-v034"]
        self.rollback_to_asgs = ["loadtest-edx-edxapp-v097", "loadtest-edx-worker-v098"]
        in_service_post_rollback_asgs = ["loadtest-edx-edxapp-v099", "loadtest-edx-worker-v099"]

        # Create the "rollback-to" ASGs.
        for asg in self.rollback_to_asgs:
            create_asg_with_tags(asg, self.test_asg_tags, self.test_ami_id, [self.test_elb_name])

        self._mock_asgard_not_pending_delete(in_service_pre_rollback_asgs, body=ENABLED_ASG)
        self._mock_asgard_pending_delete(not_in_service_asgs)

        for asg in in_service_post_rollback_asgs:
            url = asgard.ASG_INFO_URL.format(asg)
            httpretty.register_uri(
                httpretty.GET,
                url,
                responses=[
                    # Start disabled and end enabled.
                    httpretty.Response(body=DISABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=DISABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                    httpretty.Response(body=ENABLED_ASG.format(asg),
                                       content_type="application/json",
                                       status=200),
                ]
            )

        cluster = "app_cluster"
        httpretty.register_uri(
            httpretty.GET,
            asgard.CLUSTER_INFO_URL.format(cluster),
            body=VALID_CLUSTER_JSON_INFO,
            content_type="application/json"
        )

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_rollback_with_failure_and_with_redeploy(self):
        self._setup_rollback()

        # The pending delete of the ASGs to rollback to causes the rollback to fail.
        self._mock_asgard_pending_delete(self.rollback_to_asgs)

        rollback_input = {
            'current_asgs': {
                'loadtest-edx-edxapp':
                    [
                        'loadtest-edx-edxapp-v058',
                        'loadtest-edx-edxapp-v059'
                    ],
                'loadtest-edx-worker': ['loadtest-edx-worker-v034']
            },
            'rollback_to_asgs': {
                'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v097'],
                'loadtest-edx-worker': ['loadtest-edx-worker-v098']
            },
        }
        expected_output = {
            'ami_id': self.test_ami_id,
            'current_asgs':
                {
                    'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v099'],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v099']
                },
            'disabled_asgs':
                {
                    'loadtest-edx-edxapp':
                        [
                            'loadtest-edx-edxapp-v058',
                            'loadtest-edx-edxapp-v059'
                        ],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v034'],
                },
        }

        # Rollback and check output.
        self.assertEqual(
            asgard.rollback(rollback_input['current_asgs'], rollback_input['rollback_to_asgs'], self.test_ami_id),
            expected_output
        )

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    def test_rollback_with_failure_and_without_redeploy(self):
        self._setup_rollback()

        # The pending delete of the ASGs to rollback to causes the rollback to fail.
        self._mock_asgard_pending_delete(self.rollback_to_asgs)

        rollback_input = {
            'current_asgs':
                {
                    'loadtest-edx-edxapp':
                        [
                            'loadtest-edx-edxapp-v058',
                            'loadtest-edx-edxapp-v059'
                        ],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v034']
                },
            'rollback_to_asgs':
                {
                    'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v097'],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v098']
                },
        }
        expected_output = {
            'ami_id': None,
            'current_asgs':
                {
                    'loadtest-edx-edxapp':
                        [
                            'loadtest-edx-edxapp-v058',
                            'loadtest-edx-edxapp-v059'
                        ],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v034']
                },
            'disabled_asgs':
                {
                    'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v097'],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v098']
                },
        }

        # Rollback and check output.
        # No AMI ID specified - so no deploy occurs after the rollback failure.
        self.assertEqual(
            asgard.rollback(rollback_input['current_asgs'], rollback_input['rollback_to_asgs']),
            expected_output
        )

    @httpretty.activate
    @mock_autoscaling
    @mock_ec2
    @mock_elb
    @mock.patch('boto.ec2.autoscale.AutoScaleConnection.delete_tags', lambda *args: None)
    def test_rollback_with_failure_and_asgs_tagged_for_deletion(self):
        self._setup_rollback()

        tag_asg_for_deletion('loadtest-edx-edxapp-v097', -2000)
        tag_asg_for_deletion('loadtest-edx-worker-v098', -2000)
        self._mock_asgard_not_pending_delete(self.rollback_to_asgs, body=ENABLED_ASG)

        rollback_input = {
            'current_asgs':
                {
                    'loadtest-edx-edxapp':
                        [
                            'loadtest-edx-edxapp-v058',
                            'loadtest-edx-edxapp-v059'
                        ],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v034']
                },
            'rollback_to_asgs':
                {
                    'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v097'],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v098']
                },
        }
        # Deletion tags are removed from 97/98 and they're used for the rollback.
        expected_output = {
            'ami_id': self.test_ami_id,
            'current_asgs':
                {
                    'loadtest-edx-edxapp': ['loadtest-edx-edxapp-v097'],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v098']
                },
            'disabled_asgs':
                {
                    'loadtest-edx-edxapp':
                        [
                            'loadtest-edx-edxapp-v058',
                            'loadtest-edx-edxapp-v059'
                        ],
                    'loadtest-edx-worker': ['loadtest-edx-worker-v034']
                },
        }

        # Rollback and check output.
        self.assertEqual(
            asgard.rollback(rollback_input['current_asgs'], rollback_input['rollback_to_asgs'], self.test_ami_id),
            expected_output
        )

    @httpretty.activate
    def test_is_asg_pending_delete(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_pending_delete([asg])
        self.assertTrue(asgard.is_asg_pending_delete(asg))

    @httpretty.activate
    def test_is_asg_not_pending_delete(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_not_pending_delete([asg])
        self.assertFalse(asgard.is_asg_pending_delete(asg))

    @data((DISABLED_ASG, "loadtest-edx-edxapp-v060", False), (ENABLED_ASG, "loadtest-edx-edxapp-v060", True))
    @httpretty.activate
    @unpack
    def test_is_asg_enabled(self, response_body, asg_name, expected_return):
        url = asgard.ASG_INFO_URL.format(asg_name)
        httpretty.register_uri(
            httpretty.GET,
            url,
            body=response_body.format(asg_name),
            content_type="application/json")
        self.assertEqual(asgard.is_asg_enabled(asg_name), expected_return)

    @httpretty.activate
    def test_is_asg_enabled_deleted_asg(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_not_pending_delete([asg], 404)
        self.assertEqual(asgard.is_asg_enabled(asg), False)

    @httpretty.activate
    def test_get_asg_info(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_not_pending_delete([asg])
        self.assertEqual(asgard.get_asg_info(asg), json.loads(DELETED_ASG_NOT_IN_PROGRESS.format(asg)))

    @httpretty.activate
    def test_get_asg_info_html_response(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_not_pending_delete([asg], html_return=True)
        with self.assertRaises(BackendError):
            asgard.get_asg_info(asg)

    @httpretty.activate
    def test_get_asg_info_404(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_pending_delete([asg], 404)
        with self.assertRaises(ASGDoesNotExistException) as context_manager:
            asgard.get_asg_info(asg)
        error_message = "Autoscale group {} does not exist".format(asg)
        self.assertEqual(context_manager.exception.message, error_message)

    @httpretty.activate
    def test_get_asg_info_500(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_pending_delete([asg], 500)
        with self.assertRaises(BackendError) as context_manager:
            asgard.get_asg_info(asg)
        self.assertTrue(context_manager.exception.message.startswith("Asgard experienced an error:"))

    @httpretty.activate
    def test_get_asg_info_403(self):
        asg = "loadtest-edx-edxapp-v060"
        self._mock_asgard_pending_delete([asg], 403)
        with self.assertRaises(BackendError) as context_manager:
            asgard.get_asg_info(asg)
        error_message = "Call to asgard failed with status code: {}".format(403)
        self.assertTrue(context_manager.exception.message.startswith(error_message))
