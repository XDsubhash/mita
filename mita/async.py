import pecan
from celery import Celery
from datetime import timedelta, datetime
import requests
import jenkins
import json
import os
import logging
import warnings
from sqlalchemy.exc import InvalidRequestError
from mita import util, models, connections, providers
from mita.exceptions import CloudNodeNotFound
from celery.signals import worker_init

from pecan.configuration import Config

try:
    from logging.config import dictConfig as load_logging_config
except ImportError:
    from logutils.dictconfig import dictConfig as load_logging_config  # noqa

logger = logging.getLogger(__name__)


def configure_celery_logging():
    logging = pecan.conf.get('logging', {})
    debug = pecan.conf.get('debug', False)
    if logging:
        if debug:
            try:
                #
                # By default, Python 2.7+ silences DeprecationWarnings.
                # However, if conf.app.debug is True, we should probably ensure
                # that users see these types of warnings.
                #
                from logging import captureWarnings
                captureWarnings(True)
                warnings.simplefilter("default", DeprecationWarning)
            except ImportError:
                # No captureWarnings on Python 2.6, DeprecationWarnings are on
                pass

        if isinstance(logging, Config):
            logging = logging.to_dict()
        if 'version' not in logging:
            logging['version'] = 1
        load_logging_config(logging)

@worker_init.connect
def bootstrap_pecan(signal, sender, **kw):
    try:
        config_path = os.environ['PECAN_CONFIG']
    except KeyError:
        here = os.path.abspath(os.path.dirname(__file__))
        config_path = os.path.abspath(os.path.join(here, '../config/config.py'))

    pecan.configuration.set_config(config_path, overwrite=True)
    configure_celery_logging()
    # Once configuration is set we need to initialize the models so that we can connect
    # to the DB wth a configured mapper.
    models.init_model()


app = Celery('mita.async', broker='pyamqp://guest@localhost//', include=['mita.tasks'])


@app.task
def check_idling():
    """
    Idling machines that have been lazy for N (configurable) minutes (defaults
    to 10) will get removed from Jenkins, the mita database, and the cloud
    provider (instance will get terminated).

    The API does not provide information about idle time for a slave, nor it
    can tell when was the last time it built something so we are required to
    check for the ``idle`` key and if ``True`` then record that the node is in
    idle mode with a timestamp on the database **if the previous state was not idling**

    Once the
    """
    jenkins_url = pecan.conf.jenkins['url']
    jenkins_user = pecan.conf.jenkins['user']
    jenkins_token = pecan.conf.jenkins['token']
    conn = jenkins.Jenkins(jenkins_url, jenkins_user, jenkins_token)
    ci_nodes = conn.get_nodes()

    # determine which nodes are nodes we have added, so that they can be processed:
    mita_nodes = [n for n in ci_nodes if len(n['name'].split('__')) > 1]

    if mita_nodes:
        logger.info('found Jenkins nodes added by this service: %s' % len(mita_nodes))
        # check if they are idle, and if so, ping the mita API so that it can handle
        # proper removal of the node if it needs to
        for n in mita_nodes:
            node_info = conn.get_node_info(n['name'])
            uuid = n['name'].split('__')[-1]
            if node_info.get('idle'):
                logging.info("found an idle node: %s" % n['name'])
                node_endpoint = get_mita_api('nodes', uuid, 'idle')
                requests.post(node_endpoint)
            else:
                logger.info('%s is not idle, reset node.idle_since' % n['name'])
                node_endpoint = get_mita_api('nodes', uuid, 'active')
                requests.post(node_endpoint)
    else:
        logger.info('no Jenkins nodes added by this service where found')


@app.task
def check_queue():
    """
    Specifically checks for the status of the Jenkins queue. The docs are
    sparse here, but
    ``jenkins/core/src/main/resources/hudson/model/queue/CauseOfBlockage`` has
    the specific reasons this check needs:

    * *BecauseLabelIsBusy* Waiting for next available executor on {0}

    * *BecauseLabelIsOffline* All nodes of label \u2018{0}\u2019 are offline

    * *BecauseNodeIsBusy* Waiting for next available executor on {0}

    * *BecauseNodeLabelIsOffline* There are no nodes with the label \u2018{0}\u2019

    * *BecauseNodeIsOffline* {0} is offline

    The distinction is the need for a label or a node. In the case of a node,
    it will get matched directly to the nodes in the configuration, in case of a label
    it will go through the configured nodes and pick the first matched to its labels.

    Label needed example
    --------------------
    Jenkins queue reports that 'All nodes of label x86_64 are offline'. The
    configuration has::

        nodes: {
            'centos6': {
                ...
                'labels': ['x86_64', 'centos', 'centos6']
            }
        }

    Since 'x86_64' exists in the 'centos6' configured node, it will be sent off
    to create that one.

    Node needed example
    -------------------
    Jenkins reports that 'wheezy is offline'. The configuration has a few
    labels configured::

        nodes: {
            'wheezy': {
                ...
            }
            'centos6': {
                ...
            }
        }

    Since the key 'wheezy' matches the node required by the build system to
    continue it goes off to create it.
    """
    jenkins_url = pecan.conf.jenkins['url']
    jenkins_user = pecan.conf.jenkins['user']
    jenkins_token = pecan.conf.jenkins['token']
    conn = jenkins.Jenkins(jenkins_url, jenkins_user, jenkins_token)
    result = conn.get_queue_info()
    needed_nodes = {}

    if result:
        for task in result:
            if task['why'] is None:
                # this may happen when multiple tasks are getting pilled up (for a PR for example)
                # and there is no 'why' yet. So the API has a `None` for it which would break logic
                # to infer what is needed to get it unstuck
                continue
            if util.is_stuck(task['why']):
                logger.info('found stuck task with name: %s' % task['task']['name'])
                logger.info('reason was: %s' % task['why'])
                node_name = util.match_node(task['why'])
                if not node_name:
                    # this usually happens when jenkins is waiting on an executor
                    # on a static slave whose labels are not a subset of a
                    # node in the mita config
                    logger.warning('unable to match a suitable node')
                    logger.warning('will infer from job labels')
                    job_url = task['task']['url']
                    job_name = util.job_from_url(job_url)
                    try:
                        conn.get_job_info(job_name)
                        logger.info("Job info found for: %s", job_name)
                        node_name = util.match_node_from_job_config(job_url)
                    except jenkins.JenkinsException:
                        logger.warning('No job info found for: %s', job_name)
                        logger.warning('Will assume the job is a matrix job')
                        node_name = util.match_node_from_matrix_job_name(job_name)
                    if not node_name:
                        logger.warning('completely unable to match a node to provide')
                        continue
                logger.info('inferred node as: %s' % str(node_name))
                if node_name:
                    logger.info('matched a node name to config: %s' % node_name)
                    # TODO: this should talk to the pecan app over HTTP using
                    # the `app.conf.pecan_app` configuration entry, and then follow this logic:
                    # * got asked to create a new node -> check for an entry in the DB for a node that
                    # matches the characteristics of it.
                    #  * if there is one already:
                    #    - check that if it has been running for more than N (configurable) minutes (defaults to 8):
                    #      * if it has, it means that it is probable busy already, so:
                    #        - create a new node in the cloud backend matching the characteristics needed
                    #      * if it hasn't, it means that it is still getting provisioned so:
                    #        - skip - do a log warning
                    #  * if there is more than one, and it has been more than
                    #    N (8) minutes since they got launched it is possible
                    #    that they are configured *incorrectly* and we should not
                    #    keep launching more, so log the warning and skip.
                    #    - now ask Jenkins about machines that have been idle
                    #      for N (configurable) minutes, and see if matches
                    #      a record in the DB for the characteristics that we are
                    #      being asked to create.
                    #      * if found/matched:
                    #        - log the warnings again, something is not working right.
                    if needed_nodes.get(node_name):
                        needed_nodes[node_name] += 1
                    else:
                        needed_nodes[node_name] = 1
                else:
                    logger.warning('could not match a node name to config for labels')
            else:
                logger.info('no tasks where fund in "stuck" state')
    elif result == []:
        logger.info('the Jenkins queue is empty, nothing to do')
    else:
        logger.warning('attempted to get queue info but got: %s' % result)
    # At this point we might have a bag of nodes that we need to create, go over that
    # mapping and ask as many as Jenkins needs:
    node_endpoint = get_mita_api('nodes')
    for node_name, count in needed_nodes.items():
        configured_node = pecan.conf.nodes[node_name]
        configured_node['name'] = node_name
        configured_node['count'] = count
        requests.post(node_endpoint, data=json.dumps(configured_node))


@app.task
def check_orphaned():
    """
    Machines created in providers might be in an error state or some
    configuration in between may have prevented them to join Jenkins (or
    manually removed). This task will go through the nodes it knows about, make
    sure they exist in the provider and if so, remove them from the mita
    database and the provider.
    """
    conn = connections.jenkins_connection()
    try:
        nodes = models.Node.query.all()
    except InvalidRequestError:
        logger.exception('could not list nodes')
        models.rollback()
        # we can try again at the next scheduled task run
        return

    for node in nodes:
        # it is all good if this node exists in Jenkins. That is the whole
        # reason for its miserable existence, to work for Mr. Jenkins. Let it
        # be.
        if node.jenkins_name:
            if conn.node_exists(node.jenkins_name):
                continue
        # So this node is not in Jenkins. If it is less than 15 minutes then
        # don't do anything because it might be just taking a while to join.
        # ALERT MR ROBINSON: 15 minutes is a magical number.
        now = datetime.utcnow()
        difference = now - node.created
        if difference.seconds > 900:  # magical number alert
            logger.info("found created node that didn't join Jenkins: %s", node)
            provider = providers.get(node.provider)
            # "We often miss opportunity because it's dressed in overalls and
            # looks like work". Node missed his opportunity here.
            try:
                provider.destroy_node(name=node.cloud_name)
            except CloudNodeNotFound:
                logger.info("cloud was not found on provider: %s", node.cloud_name)
                logger.info("will remove node from database, API confirms it no longer exists")
                node.delete()
                models.commit()
            except Exception:
                logger.exception("unable to destroy node: %s", node.cloud_name)
                logger.error("will skip database removal")
                continue

    # providers can purge nodes in error state too, try to prune those as well
    providers_conf =  pecan.conf.provider.to_dict()
    for provider_name in providers_conf.keys():
        provider = providers.get(provider_name)
        provider.purge()


def get_mita_api(endpoint=None, *args):
    """
    Puts together the API url for mita, so that we can talk to it. Optionally, the endpoint
    argument allows to return the correct url for specific needs. For example, to create a node:

        http://0.0.0.0/api/nodes/

    """
    server = pecan.conf['server']['host']
    port = pecan.conf['server']['port']
    base = "http://%s:%s/api" % (server, port)
    endpoints = {
        'nodes': '%s/nodes/' % base,
    }
    url = base
    if endpoint:
        url = endpoints[endpoint]

    if args:
        for part in args:
            url = os.path.join(url, part)
    return url


app.conf.update(
    CELERYBEAT_SCHEDULE={
        'check-orphaned-every-120-seconds': {
            'task': 'async.check_orphaned',
            'schedule': timedelta(seconds=120),
        },
        'check-idle-every-30-seconds': {
            'task': 'async.check_idling',
            'schedule': timedelta(seconds=30),
        },
        'add-every-30-seconds': {
            'task': 'async.check_queue',
            'schedule': timedelta(seconds=30),
        },
    },
)
