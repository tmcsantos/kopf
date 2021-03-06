"""
Peer monitoring: knowing which other operators do run, and exchanging the basic signals with them.

The main use-case is to suppress all deployed operators when a developer starts a dev-/debug-mode
operator for the same cluster on their workstation -- to avoid any double-processing.

See also: `kopf freeze` & `kopf resume` CLI commands for the same purpose.

WARNING: There are **NO** per-object locks between the operators, so only one operator
should be functional for the cluster, i.e. only one with the highest priority running.
If the operator sees the violations of this constraint, it will print the warnings
pointing to another same-priority operator, but will continue to function.

The "signals" exchanged are only the keep-alive notifications from the operator being alive,
and detection of other operators hard termination (by timeout rather than by clear exit).

The peers monitoring covers both the in-cluster operators running,
and the dev-mode operators running in the dev workstations.

For this, special CRDs ``kind: ClusterKopfPeering`` & ``kind: KopfPeering``
should be registered in the cluster, and their ``status`` field is used
by all the operators to sync their keep-alive info.

The namespace-bound operators (e.g. `--namespace=`) report their individual
namespaces are part of the payload, can see all other cluster and namespaced
operators (even from the different namespaces), and behave accordingly.

The CRD is not applied automatically, so you have to deploy it yourself explicitly.
To disable the peers monitoring, use the `--standalone` CLI option.
"""

import asyncio
import datetime
import getpass
import logging
import os
import random
from typing import Any, Dict, Iterable, Mapping, NewType, NoReturn, Optional, Union, cast

import iso8601

from kopf.clients import fetching, patching
from kopf.structs import bodies, configuration, patches, primitives, resources
from kopf.utilities import hostnames

logger = logging.getLogger(__name__)

# The CRD info on the special sync-object.
CLUSTER_PEERING_RESOURCE = resources.Resource('zalando.org', 'v1', 'clusterkopfpeerings')
NAMESPACED_PEERING_RESOURCE = resources.Resource('zalando.org', 'v1', 'kopfpeerings')

Identity = NewType('Identity', str)


# The class used to represent a peer in the parsed peers list (for convenience).
# The extra fields are for easier calculation when and if the peer is dead to the moment.
class Peer:

    def __init__(
            self,
            *,
            identity: Identity,
            priority: int = 0,
            lastseen: Optional[Union[str, datetime.datetime]] = None,
            lifetime: Union[int, datetime.timedelta] = 60,
            **_: Any,  # for the forward-compatibility with the new fields
    ):
        super().__init__()
        self.identity = identity
        self.priority = priority
        self.lifetime = (lifetime if isinstance(lifetime, datetime.timedelta) else
                         datetime.timedelta(seconds=int(lifetime)))
        self.lastseen = (lastseen if isinstance(lastseen, datetime.datetime) else
                         iso8601.parse_date(lastseen) if lastseen is not None else
                         datetime.datetime.utcnow())
        self.lastseen = self.lastseen.replace(tzinfo=None)  # only the naive utc -- for comparison
        self.deadline = self.lastseen + self.lifetime
        self.is_dead = self.deadline <= datetime.datetime.utcnow()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(identity={self.identity}, priority={self.priority}, lastseen={self.lastseen}, lifetime={self.lifetime})"

    def as_dict(self) -> Dict[str, Any]:
        # Only the non-calculated and non-identifying fields.
        return {
            'priority': self.priority,
            'lastseen': self.lastseen.isoformat(),
            'lifetime': self.lifetime.total_seconds(),
        }


async def process_peering_event(
        *,
        raw_event: bodies.RawEvent,
        freeze_mode: primitives.Toggle,
        namespace: Optional[str],
        identity: Identity,
        settings: configuration.OperatorSettings,
        autoclean: bool = True,
        replenished: asyncio.Event,
) -> None:
    """
    Handle a single update of the peers by us or by other operators.

    When an operator with a higher priority appears, switch to the freeze-mode.
    The these operators disappear or become presumably dead, resume the event handling.

    The freeze object is passed both to the peers handler to set/clear it,
    and to all the resource handlers to check its value when the events arrive
    (see `create_tasks` and `run` functions).
    """
    body: bodies.RawBody = raw_event['object']
    meta: bodies.RawMeta = raw_event['object']['metadata']

    # Silently ignore the peering objects which are not ours to worry.
    if meta.get('namespace') != namespace or meta.get('name') != settings.peering.name:
        return

    # Find if we are still the highest priority operator.
    pairs = cast(Mapping[str, Mapping[str, object]], body.get('status', {}))
    peers = [Peer(identity=Identity(opid), **opinfo) for opid, opinfo in pairs.items()]
    dead_peers = [peer for peer in peers if peer.is_dead]
    live_peers = [peer for peer in peers if not peer.is_dead and peer.identity != identity]
    prio_peers = [peer for peer in live_peers if peer.priority > settings.peering.priority]
    same_peers = [peer for peer in live_peers if peer.priority == settings.peering.priority]

    if autoclean and dead_peers:
        await clean(peers=dead_peers, settings=settings, namespace=namespace)

    if prio_peers:
        if freeze_mode.is_off():
            logger.info(f"Freezing operations in favour of {prio_peers}.")
            await freeze_mode.turn_on()
    elif same_peers:
        logger.warning(f"Possibly conflicting operators with the same priority: {same_peers}.")
        if freeze_mode.is_off():
            logger.warning(f"Freezing all operators, including self: {peers}")
            await freeze_mode.turn_on()
    else:
        if freeze_mode.is_on():
            logger.info(f"Resuming operations after the freeze. Conflicting operators with the same priority are gone.")
            await freeze_mode.turn_off()


async def keepalive(
        *,
        namespace: Optional[str],
        identity: Identity,
        settings: configuration.OperatorSettings,
) -> NoReturn:
    """
    An ever-running coroutine to regularly send our own keep-alive status for the peers.
    """
    try:
        while True:
            await touch(
                identity=identity,
                settings=settings,
                namespace=namespace,
            )

            # How often do we update. Keep limited to avoid k8s api flooding.
            # Should be slightly less than the lifetime, enough for a patch request to finish.
            await asyncio.sleep(max(1, int(settings.peering.lifetime - 10)))
    finally:
        try:
            await asyncio.shield(touch(
                identity=identity,
                settings=settings,
                namespace=namespace,
                lifetime=0,
            ))
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"Couldn't remove self from the peering. Ignoring.")


async def touch(
        *,
        identity: Identity,
        settings: configuration.OperatorSettings,
        namespace: Optional[str],
        lifetime: Optional[int] = None,
) -> None:
    name = settings.peering.name
    resource = guess_resource(namespace=namespace)

    peer = Peer(
        identity=identity,
        priority=settings.peering.priority,
        lifetime=settings.peering.lifetime if lifetime is None else lifetime,
    )

    patch = patches.Patch()
    patch.update({'status': {identity: None if peer.is_dead else peer.as_dict()}})
    rsp = await patching.patch_obj(resource=resource, namespace=namespace, name=name, patch=patch)

    if not settings.peering.stealth or rsp is None:
        where = f"in {namespace!r}" if namespace else "cluster-wide"
        result = "not found" if rsp is None else "ok"
        logger.debug(f"Keep-alive in {name!r} {where}: {result}.")


async def clean(
        *,
        peers: Iterable[Peer],
        settings: configuration.OperatorSettings,
        namespace: Optional[str],
) -> None:
    name = settings.peering.name
    resource = guess_resource(namespace=namespace)

    patch = patches.Patch()
    patch.update({'status': {peer.identity: None for peer in peers}})
    await patching.patch_obj(resource=resource, namespace=namespace, name=name, patch=patch)


async def detect_presence(
        *,
        settings: configuration.OperatorSettings,
        namespace: Optional[str],
) -> Optional[bool]:

    if settings.peering.standalone:
        return None

    resource = guess_resource(namespace=namespace)
    name = settings.peering.name
    obj = await fetching.read_obj(resource=resource, namespace=namespace, name=name, default=None)
    if settings.peering.mandatory and obj is None:
        raise Exception(f"The mandatory peering {name!r} was not found.")
    elif obj is None:
        logger.warning(f"Default peering object is not found, falling back to the standalone mode.")
        return False
    else:
        return True


def detect_own_id(*, manual: bool) -> Identity:
    """
    Detect or generate the id for ourselves, i.e. the execute operator.

    It is constructed easy to detect in which pod it is running
    (if in the cluster), or who runs the operator (if not in the cluster,
    i.e. in the dev-mode), and how long ago was it started.

    The pod id can be specified by::

        env:
        - name: POD_ID
          valueFrom:
            fieldRef:
              fieldPath: metadata.name

    Used in the `kopf.reactor.queueing` when the reactor starts,
    but is kept here, close to the rest of the peering logic.
    """

    pod = os.environ.get('POD_ID', None)
    if pod is not None:
        return Identity(pod)

    user = getpass.getuser()
    host = hostnames.get_descriptive_hostname()
    now = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rnd = ''.join(random.choices('abcdefhijklmnopqrstuvwxyz0123456789', k=3))
    return Identity(f'{user}@{host}' if manual else f'{user}@{host}/{now}/{rnd}')


def guess_resource(namespace: Optional[str]) -> resources.Resource:
    return CLUSTER_PEERING_RESOURCE if namespace is None else NAMESPACED_PEERING_RESOURCE
