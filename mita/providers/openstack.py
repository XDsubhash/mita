import logging
from libcloud.compute.types import Provider
from libcloud.compute.providers import get_driver
from time import sleep
import socket
from ssl import SSLError
import libcloud.security
from pecan import conf

from mita.exceptions import CloudNodeNotFound

logger = logging.getLogger(__name__)

# libcloud does not have a timeout enabled for Openstack calls to
# ``create_node``, and it uses the default timeout value from socket which is
# ``None`` (meaning: it will wait forever). This setting will set the default
# to a magical number, which is 280 (4 minutes). This is 1 minute less than the
# timeouts for production settings that should allow enough time to handle the
# exception and return a response
socket.setdefaulttimeout(280)

# FIXME
# At the time this example was written, https://nova-api.trystack.org:5443
# was using a certificate issued by a Certificate Authority (CA) which is
# not included in the default Ubuntu certificates bundle (ca-certificates).
# Note: Code like this poses a security risk (MITM attack) and that's the
# reason why you should never use it for anything else besides testing. You
# have been warned.
libcloud.security.VERIFY_SSL_CERT = False

OpenStack = get_driver(Provider.OPENSTACK)


def get_driver():
    driver = OpenStack(
        conf.provider.openstack.username,
        conf.provider.openstack.password,
        ex_force_auth_url=conf.provider.openstack.auth_url,
        ex_force_auth_version=conf.provider.openstack.auth_version,
        ex_tenant_name=conf.provider.openstack.tenant_name,
        ex_force_service_region=conf.provider.openstack.service_region,
    )
    return driver


def purge():
    """
    Get rid of nodes in Error state
    """
    driver = get_driver()
    nodes = driver.list_nodes()
    logger.info('looking for nodes in error state for removal')
    destroyed = 0
    for node in nodes:
        # it used to be the case that 'state' would be an integer, and that
        # OVH would slap a 7 for a node in ERROR. Somehow the __repr__ of the object
        # will contain that, so try that too
        error_integer = node.state == 7
        error_repr = 'state=ERROR' in str(node)
        error_string = node.state == 'error'
        if error_integer or error_repr or error_string:
            destroyed += 1
            logger.info('destroying node in error state: %s', str(node))
            node.destroy()
    if destroyed:
        logger.warning('%s nodes destroyed that were found in error state' % destroyed)
        return True
    logger.info('no nodes found in error state, nothing was destroyed')


def create_node(**kw):
    name = kw['name']
    driver = get_driver()
    images = driver.list_images()
    sizes = driver.list_sizes()
    available_sizes = [s for s in sizes if s.name == kw['size']]

    if not available_sizes:
        logger.error("provider does not have a matching 'size' for %s", kw['size'])
        logger.error(
            "no vm will be created. Ensure that '%s' is an available size and that it exists",
            kw['size']
        )
        return

    storage = kw.get("storage")

    size = available_sizes[0]
    matching_images = [i for i in images if i.name == kw['image_name']]

    if not matching_images:
        logger.error("provider does not have a matching 'image_name' for %s", kw['image_name'])
        logger.error(
            "no vm will be created. Ensure that '%s' is an available image and that it exists",
            kw['image_name']
        )
        return

    image = matching_images[0]

    try:
        new_node = driver.create_node(
            name=name, image=image, size=size,
            ex_userdata=kw['script'], ex_keyname=kw['keyname']
        )
    except SSLError:
        new_node = None
        logger.error("failed to connect to provider, probably a timeout was reached")

    if not new_node:
        logger.error("provider could not create node with details: %s", str(kw))
        return

    logger.info("created node: %s", new_node)

    if storage:
        logger.info("Creating %sgb of storage for: %s", storage, name)
        new_volume = driver.create_volume(storage, name)
        # wait for the new volume to become available
        logger.info("Waiting for volume %s to become available", name)
        _wait_until_volume_available(new_volume, maybe_in_use=True)
        # wait for the new node to become available
        logger.info("Waiting for node %s to become available", name)
        driver.wait_until_running([new_node])
        logger.info(" ... available")
        logger.info("Attaching volume %s...", name)
        if driver.attach_volume(new_node, new_volume, '/dev/vdb') is not True:
            raise RuntimeError("Could not attach volume %s" % name)
        logger.info("Successfully attached volume %s", name)


def _wait_until_volume_available(volume, maybe_in_use=False):
    """
    Wait until a StorageVolume's state is "available".
    Set "maybe_in_use" to True in order to wait even when the volume is
    currently in_use. For example, set this option if you're recycling
    this volume from an old node that you've very recently
    destroyed.

    This is the value of driver.VOLUME_STATE_MAP from libcloud as reference::

        {'attaching': 7,
         'available': 0,
         'backing-up': 6,
         'creating': 3,
         'deleting': 4,
         'error': 1,
         'error_deleting': 1,
         'error_extending': 1,
         'error_restoring': 1,
         'in-use': 2,
         'restoring-backup': 6}
    """
    ok_states = ['creating', 3]  # it's ok to wait if the volume is in this
    tries = 0
    if maybe_in_use:
        ok_states.append('in_use')
    logger.info('Volume: %s is in state: %s', volume.name, volume.state)
    while volume.state in ok_states:
        sleep(3)
        volume = get_volume(volume.name)
        tries = tries + 1
        if tries > 10:
            logger.info("Maximum amount of tries reached..")
            break
        if volume.state == 'notfound':
            logger.error('no volume was found for: %s', volume.name)
            break
        logger.info(' ... %s', volume.state)
    if volume.state not in ['available', 0]:
        # OVH uses a non-standard state of 0 to indicate an available volume
        logger.info('Volume %s is %s (not available)', volume.name, volume.state)
        logger.info('The volume %s is not available, but will continue anyway...', volume.name)
    return True


class UnavailableVolume(object):
    """
    If a Volume is not found, this object will return to maintain compatibility
    with actual (correct) StorageVolume objects from libcloud.

    Note that the 'notfound' state does not comply directly with
    `StorageVolumeState` but it is used internally to determine the inability
    to find the correct volume.
    """

    def __init__(self, name, state='notfound'):
        self.name = name
        self.state = state


def get_volume(name):
    """ Return libcloud.compute.base.StorageVolume """
    driver = get_driver()
    volumes = driver.list_volumes()
    try:
        return [v for v in volumes if v.name == name][0]
    except IndexError:
        return UnavailableVolume(name)


def destroy_node(**kw):
    """
    Relies on the fact that names **should be** unique. Along the chain we
    prevent non-unique names to be used/added.
    TODO: raise an exception if more than one node is matched to the name, that
    can be propagated back to the client.
    """
    driver = get_driver()
    name = kw['name']
    nodes = driver.list_nodes()
    for node in nodes:
        if node.name == name:
            try:
                result = driver.destroy_node(node)
                if not result:
                    raise RuntimeError('API failed to destroy node: %s', name)
                destroy_volume(name)
                return
            except Exception:
                logger.exception('unable to destroy_node: %s', name)
                raise

    raise CloudNodeNotFound


def destroy_volume(name):
    driver = get_driver()
    volume = get_volume(name)
    # check to see if this is a valid volume
    if volume.state != "notfound":
        logger.info("Destroying volume %s", name)
        driver.destroy_volume(volume)
