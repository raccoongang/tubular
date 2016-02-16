from datetime import datetime,timedelta
import os
import logging
import requests
import time
from requests.exceptions import ConnectionError
from collections import Iterable
from .exception import *


ASGARD_API_ENDPOINT = os.environ.get("ASGARD_API_ENDPOINTS", "http://dummy.url:8091")
ASGARD_API_TOKEN = os.environ.get("ASGARD_API_TOKEN")

CLUSTER_LIST_URL= "{}/cluster/list.json".format(ASGARD_API_ENDPOINT)
ASG_ACTIVATE_URL= "{}/cluster/activate".format(ASGARD_API_ENDPOINT)
ASG_DEACTIVATE_URL= "{}/cluster/deactivate".format(ASGARD_API_ENDPOINT)
NEW_ASG_URL= "{}/cluster/createNextGroup".format(ASGARD_API_ENDPOINT)
ASG_INFO_URL="{}/autoScaling/show/{}.json".format(ASGARD_API_ENDPOINT, "{}")
CLUSTER_INFO_URL = "{}/cluster/show/{}.json".format(ASGARD_API_ENDPOINT, "{}")

LOG = logging.getLogger(__name__)

def clusters_for_asgs(asgs):
    """
    An autoscaling group can belong to multiple clusters potentially.

    This function finds all asgard clusters for a list of ASGs.
    eg. get all clusters that have the 'edxapp' cluster tag..

    Input:
        asgs(iterable): A iterable of ASGs we care about.
    eg.
    [
        u'test-edx-edxapp-v007',
        u'test-edx-worker-v007',
    ]


    Returns:
        dict: A mapping of cluster names to asgs in the cluster.

    eg.
    {
        u'test-edx-edxapp': [
            u'test-edx-edxapp-v007',
        ],
        u'test-edx-worker': [
            u'test-edx-worker-v004',
        ]
    }

    Raises:
        BackendDataError: We got bad data from the backend. We can't
            get cluster information from it.


    """

    request = requests.Request('GET', CLUSTER_LIST_URL, params=ASGARD_API_TOKEN)
    url = request.prepare().url
    LOG.debug("Getting Cluster List from: {}".format(url))
    response = requests.get(CLUSTER_LIST_URL, params=ASGARD_API_TOKEN)
    cluster_json = response.json()

    # need this to be a list so that we can test membership.
    asgs = list(asgs)

    relevant_clusters = {}
    for cluster in cluster_json:
        if "autoScalingGroups" not in cluster or "cluster" not in cluster:
            msg = "Expected 'cluster' and 'autoScalingGroups' keys in dict: {}".format(cluster)
            raise BackendDataError(msg)

        for asg in cluster['autoScalingGroups']:
            LOG.debug("Membership: {} in {}: {}".format(asg, asgs, asg in asgs))
            if asg in asgs:
                relevant_clusters[cluster['cluster']] = cluster['autoScalingGroups']
                # A cluster can have multiple relevant ASGs.
                # We don't need to check them all.
                break # The inner for loop

    return relevant_clusters

def asgs_for_cluster(cluster):
    """
    Given a named cluster, get all ASGs in the cluster.

    Input:
        cluster(str): The name of the asgard cluster.

    Returns:
        list: List of ASGs.
    """

    LOG.debug("URL: {}".format(CLUSTER_INFO_URL.format(cluster)))
    url = CLUSTER_INFO_URL.format(cluster)
    response = requests.get(url, params=ASGARD_API_TOKEN)

    LOG.debug("ASGs for Cluster: {}".format(response.text))
    asgs = response.json()

    try:
        asg_names = map(lambda x: x['autoScalingGroupName'], asgs)
    except (KeyError,TypeError) as e:
        msg = "Expected a list of dicts with an 'autoScalingGroupName' attribute. " \
              "Got: {}".format(asgs)
        raise BackendDataError(msg)

    return asg_names

def wait_for_task_completion(task_url, timeout):
    """
    Input:
        task_url(str): The URL from which to retrieve task status.
        timeout(int): How many seconds to wait for task completion 
                      before throwing an error.

    Returns:
        (dict): Parsed json of the task completion or failure status.

    Raises:
        TimeoutException: When we timeout waiting for the task to finish.
    """

    if not task_url.endswith('.json'):
        task_url += ".json"

    LOG.debug("Task URL: {}".format(task_url))
    end_time = datetime.utcnow() + timedelta(seconds=timeout)
    while end_time > datetime.utcnow():
        response = requests.get(task_url, params=ASGARD_API_TOKEN)
        print("Wait response: {}".format(response.text))
        status = response.json()['status']
        if status == 'completed' or status == 'failed':
            return response.json()
        time.sleep(1)

    raise TimeoutException("Timedout while waiting for task {}".format(task_url))

