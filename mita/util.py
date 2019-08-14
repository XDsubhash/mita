from xml.etree import ElementTree
from libcloud.compute import types
from jenkins import NotFoundException as JenkinsNotFoundException
from pecan import conf
import logging
import os
import requests

from mita.connections import jenkins_connection
from mita.exceptions import CloudNodeNotFound
from mita.label_eval import matching_nodes


logger = logging.getLogger(__name__)


def sanitize_string(string, strip=False):
    """
    Remove all surrounding whitespace, and all utf-8 chars that are usually
    present coming from Jenkins
    """
    to_remove = [u'\u2018', u'\u2019']
    for i in to_remove:
        string = string.replace(i, '')
    if strip:
        string = string.strip()
    return string


def job_from_url(url):
    """
    Infer the ``job_name`` from a Jenkins url
    """
    return url.strip('/').split('/')[-1]


def node_state_map():
    """
    creates a forward and reverse mapping of node states assumes values for
    states are unique integers

    we need a way to map a state *name* to its value and back and libcloud
    doesn't do this for us unless we call its tostring() and fromstring()
    methods but that is ugly.
    """
    mapping = {}
    for k, v in types.NodeState.__dict__.items():
        mapping[k] = v
        mapping[v] = k
    return mapping


NodeState = node_state_map()


def get_key(_dict, key, fallback=True):
    """
    This helper is always used to check for a name (key) in configured nodes
    (_dict). The fallback should is a boolean that should be lenient and try to
    see if there is anything that will match.

    This may be because a name can be like `10.0.0.1__name__huge` but the
    configuration only has something for `name`. In which case it should try to
    match that key and return it.
    """
    if not key:
        return None
    if key in _dict:
        return key
    if fallback:
        for name in _dict.keys():
            if name in key:
                return name

# TODO: all these need proper logging
# Stuck Queue Processors


def is_stuck(string):
    """
    The Jenkins API might not cooperate with proper information, when a job is
    stuck it may be sending out messages as it was stuck, but the
    status['stuck'] will be False. This helper will check for the strings that
    mita supports for stuck jobs.
    """
    def busy_summary(string): return string.startswith('Waiting for')

    def offline_label_summary(string): return string.startswith('All nodes of label')

    def offline_node_summary(string): return string.endswith('is offline')

    def offline_node_label_summary(string): return string.startswith('There are no nodes')

    def node_does_not_have_label(string): return u"doesn\u2019t have label" in string

    for summary in [
        busy_summary,
        offline_label_summary,
        offline_node_summary,
        offline_node_label_summary,
        node_does_not_have_label
    ]:
        if summary(string) is True:
            return True
    return False


def match_node(string):
    """
    Determine what node, if any, is needed from a given state of a Jenkins
    Queue. There are three distinct states from the API, so process it and
    determine if we are able to match it to a configured node.
    """
    busy_summary = lambda string: from_label if string.startswith('Waiting for') else None
    offline_label_summary = lambda string: from_offline_label if string.startswith('All nodes of label') else None
    offline_node_summary = lambda string: from_offline_node if string.endswith('is offline') else None
    offline_node_label_summary = lambda string: from_offline_node_label if string.startswith('There are no nodes') else None
    node_does_not_have_label = lambda string: from_node_without_label if u"doesn\u2019t have label" in string else None
    for summary in [busy_summary, offline_label_summary, offline_node_summary, offline_node_label_summary, node_does_not_have_label]:
        processor = summary(string)
        if processor:
            return processor(string)


def from_label(string):
    """
    Behaves with some duality because both `BecauseLabelIsBusy` and
    `BecauseNodeIsBusy` have the same status. So it first will try to match
    a node by node name (a key in the configuration) and if it fails, it will
    go after labels.

    String to process::

        "Waiting for next available executor on {0}"

    .. note:: Although initially this function was to process a distinct type
    of string, it is being used as a generic utility function to parse other
    strings, this is why the ``string`` needs to be sanitized before
    processing.
    """
    try:
        node_or_label = string.split()[-1]
    except IndexError:
        return None
    node_or_label = sanitize_string(node_or_label)
    node_from_label = match_node_from_label(node_or_label)
    configured_nodes = get_nodes()
    # node_or_label can be a node as a key in the config, so try to get that
    # first, and use the match from labels as a fallback. Try first with no
    # sanitizing of the node name, and if that doesn't work, try by splitting
    # on possible use of '__IP'
    match = get_key(configured_nodes, node_or_label) or get_key(configured_nodes, node_from_label)
    if match is None:
        clean_node = node_or_label.split('__')[0]
        match = get_key(configured_nodes, clean_node)

    # maybe we have a label expression and not just a single label
    if match is None:
        matched_node = match_node_from_label_expr(
            node_or_label, configured_nodes
        )
        if matched_node:
            return matched_node

    # It is possible that we got a custom node name with no
    # naming conventions that would allow mita to understand
    # what it needs to be built, so go and get the labels of
    # this custom node and see if we can match them
    if match is None:
        logger.warning('unable to match: %s', node_or_label)
        logger.warning('will look at node labels and attempt a match')
        # node_or_label will now probably be a node with a custom name
        match = match_node_from_labels(get_node_labels(node_or_label))

    return match


def from_offline_node_label(string):
    """
    This is a bit difficult to process, with the configuration matrix labels
    can show as: `label&&otherlabel`, which requires parsing to understand that
    this is a possibility.

    The behavior then, is to assume that we will get a clean label (just the
    label and nothing else) and failing to do that, we must parse and verify
    that all the labels in the resulting split are contained in a configured
    host.

    String to process::

        u"There are no nodes with the label \u2018{0}\u2019"
    """
    string = sanitize_string(string)
    label = string.split()[-1]
    matched_node = match_node_from_label(label)
    if matched_node is None:
        nodes = get_nodes()
        matched_node = match_node_from_label_expr(label, nodes)
        if matched_node:
            return matched_node
    return matched_node


def from_offline_label(string):
    """
    String to process::

        u"All nodes of label \u2018{0}\u2019 are offline"
    """
    # effing unicode to have nice cute quotes in the UI
    string = sanitize_string(string)
    label = string.split()[-3]
    # first check if we get a match from a single label, e.g. 'amd64'
    single_label_match = match_node_from_label(label)
    # otherwise, fallback to multi-labels, like 'amd64&&trusty'
    if not single_label_match:
        return from_offline_node_label(label)
    return single_label_match


def from_offline_node(string):
    """
    String to process::

        "{0} is offline"
    """
    node = string.split()[0]
    configured_nodes = get_nodes()
    # node can be a node as a key in the config, so try to get that first. Try
    # first with no sanitizing of the node name, and if that doesn't work, try
    # by splitting on possible use of __IP'
    match = get_key(configured_nodes, node)
    if match is None:
        node = node.split('__')[0]
        match = node if node in configured_nodes else None
    return match


def from_offline_executor(node):
    """
    This helper does not process the string, but rather, tries to map an
    already parsed node name into something that has been configured.
    """
    if node is None:
        return None
    configured_nodes = get_nodes()
    # node can be a node as a key in the config, so try to get that first. Try
    # first with no sanitizing of the node name, and if that doesn't work, try
    # by splitting on possible use of __IP'
    match = get_key(configured_nodes, node)
    if match is None:
        node = node.split('__')[0]
        match = node if node in configured_nodes else None
    return match


def from_node_without_label(string):
    """
    String to process::

        u"... {0} doesn\u2019t have label ..."
    """
    messages = string.split(';')
    for message in messages:
        if u"doesn\u2019t have label" in message:
            node = from_label(message)
            if not node:
                continue
            return node
    logger.warning('tried to match a node without label but failed')


def match_node_from_label(label, configured_nodes=None):
    configured_nodes = configured_nodes or get_nodes()
    for node, metadata in configured_nodes.items():
        if label in metadata['labels']:
            return node


def match_node_from_labels(labels, configured_nodes=None):
    """
    Given a list of labels, map them to a configured node type so that it can
    be created. All the labels must exist in the configured node
    """
    if not labels:
        return
    configured_nodes = configured_nodes or get_nodes()

    def labels_exist(config):
        for l in labels:
            if l not in config:
                return False
        return True

    for node, metadata in configured_nodes.items():
        if labels_exist(metadata['labels']):
            return node


def match_node_from_label_expr(expr, configured_nodes=None):
    """
    Given a label expression, find all matching configured node types
    so that one can be created.
    """
    if not expr:
        return
    configured_nodes = configured_nodes or get_nodes()

    matches = matching_nodes(expr, configured_nodes)
    # XXX return first?  random?  try to figure out if
    # one is already provisioning and return it?
    if matches:
        return matches[0]
    else:
        return None


def get_nodes():
    # Note:
    # There is some odd side-effect of the pecan configuration where in
    # production you can get a ``pecan.configuration.Config`` object but in
    # tests you would get a ``pecan.configuration.ConfigDict``. The
    # configuration loading seems to be the same but it has this problem.
    # if/when this is fixed in how the celery portion of the apps loads the config then
    # this should be removed.
    try:
        return conf['nodes'].to_dict()
    except AttributeError:
        return conf['nodes']


def get_jenkins_name(uuid):
    """
    Given a node's identifier use the jenkins api to find a node name in jenkins
    that includes that uuid and return it.
    """
    conn = jenkins_connection()
    nodes = conn.get_nodes()
    for node in nodes:
        if uuid in node['name']:
            return node['name']
    return None


def get_node_labels(node_name, _xml_configuration=None):
    """
    Useful when a custom node was added with a name that mita does not
    understand due to odd/unsupported naming conventions. The Jenkins Python
    module doesn't have a nice JSON representation from the Jenkins API
    response to look at the labels, so this parses that output looking for the
    right tag and extracting the labels from there.
    """
    conn = jenkins_connection()
    node_name = node_name.encode('ascii', errors='ignore')
    try:
        xml_configuration = _xml_configuration or conn.get_node_config(node_name)
    except JenkinsNotFoundException:
        logging.warning('"%s" was not found in Jenkins', node_name)
        return []
    xml_object = ElementTree.fromstring(xml_configuration)
    for node in xml_object:
        if node.tag == 'label':
            # node labels are in this tag, parse the text. The XML should look
            # like:  <label>amd64 centos7 x86_64 huge</label>
            return node.text.split()
    return []


def delete_jenkins_node(name):
    conn = jenkins_connection()
    logger.info("Deleting node in jenkins: %s" % name)
    if not name:
        logger.info('Node does not have a jenkins_name, will skip')
        return
    if conn.node_exists(name):
        conn.delete_node(name)
        return
    logger.info("Node does not exist in Jenkins, cannot delete")


def delete_provider_node(provider, name):
    # we need to terminate this couch potato
    logger.info("Destroying cloud node: %s" % name)
    try:
        provider.destroy_node(name=name)
    except CloudNodeNotFound:
        logger.info("Node does not exist in cloud provider, cannot delete")
    except Exception:
        logger.exception("encountered errors while trying to delete node from cloud provider")


def match_node_from_job_config(job_url):
    config_url = os.path.join(job_url, "config.xml")
    logger.info("Getting job config from: %s", config_url)
    response = requests.get(config_url, auth=(conf.jenkins['user'], conf.jenkins['token']))
    try:
        response.raise_for_status()
    except Exception:
        logger.exception("failed to retrieve job config from: %s", job_url)
        return None
    element_tree = ElementTree.fromstring(response.text)
    try:
        label_expression = element_tree.find('assignedNode').text
    except AttributeError:
        logger.warning("Did not find 'assignedNode' in job config %s", job_url)
        return None
    logger.info("Found label expression: %s", label_expression)
    node = match_node_from_label_expr(label_expression)
    return node


def match_node_from_matrix_job_name(job_name):
    """
    A matrix job name will look something like:

    ARCH=x86_64,AVAILABLE_ARCH=x86_64,AVAILABLE_DIST=xenial,DIST=xenial,MACHINE_SIZE=huge
    """
    logger.info("Infering labels from matrix job_name: %s", job_name)
    labels = job_name.split(",")
    labels = [label.split("=")[1] for label in labels]
    labels = list(set(labels))
    logger.info("Found labels: %s", labels)
    node = match_node_from_labels(labels)
    return node
