"""
Daemons are background tasks accompanying the individual resource objects.

Every ``@kopf.daemon`` and ``@kopf.timer`` handler produces a separate
asyncio task to either directly execute the daemon, or to trigger one-shot
handlers by schedule. The wrapping tasks are always async; the sync functions
are called in thread executors as part of a regular handler invocation.

These tasks are remembered in the per-resources *memories* (arbitrary data
containers) through the life-cycle of the operator.

Since the operators are event-driven conceptually, there are no background tasks
running for every individual resources normally (i.e. without the daemons),
so there are no connectors between the operator's root tasks and the daemons,
so there is no way to stop/kill/cancel the daemons when the operator exits.

For this, there is an artificial root task spawned to kill all the daemons
when the operator exits, and all root tasks are gracefully/forcedly terminated.
Otherwise, all the daemons would be considered as "hung" tasks and will be
force-killed after some timeout -- which can be avoided, since we are aware
of the daemons, and they are not actually "hung".
"""
import asyncio
import time
import warnings
from typing import MutableMapping, Sequence, Collection, List

from kopf.clients import patching
from kopf.engines import logging as logging_engine
from kopf.engines import sleeping
from kopf.reactor import causation
from kopf.reactor import handling
from kopf.reactor import lifecycles
from kopf.storage import states
from kopf.structs import configuration
from kopf.structs import containers
from kopf.structs import handlers as handlers_
from kopf.structs import patches
from kopf.structs import primitives

DAEMON_POLLING_INTERVAL = 60


async def spawn_resource_daemons(
        *,
        settings: configuration.OperatorSettings,
        handlers: Sequence[handlers_.ResourceSpawningHandler],
        daemons: MutableMapping[containers.DaemonId, containers.Daemon],
        cause: causation.ResourceSpawningCause,
        memory: containers.ResourceMemory,
) -> Collection[float]:
    """
    Ensure that all daemons are spawned for this individual resource.

    This function can be called multiple times on multiple handling cycles
    (though usually should be called on the first-seen occasion), so it must
    be idempotent: not having duplicating side-effects on multiple calls.
    """
    if memory.live_fresh_body is None:  # for type-checking; "not None" is ensured in processing.
        raise RuntimeError("A daemon is spawned with None as body. This is a bug. Please report.")
    for handler in handlers:
        daemon_id = containers.DaemonId(handler.id)
        if daemon_id not in daemons:
            stopper = primitives.DaemonStopper()
            daemon_cause = causation.DaemonCause(
                resource=cause.resource,
                logger=cause.logger,
                body=memory.live_fresh_body,
                memo=memory.user_data,
                patch=patches.Patch(),  # not the same as the one-shot spawning patch!
                stopper=stopper,  # for checking (passed to kwargs)
            )
            daemon = containers.Daemon(
                stopper=stopper,  # for stopping (outside of causes)
                handler=handler,
                logger=logging_engine.LocalObjectLogger(body=cause.body, settings=settings),
                task=asyncio.create_task(_runner(
                    settings=settings,
                    handler=handler,
                    cause=daemon_cause,
                    memory=memory,
                )),
            )
            daemons[daemon_id] = daemon
    return []


async def stop_resource_daemons(
        *,
        settings: configuration.OperatorSettings,
        daemons: MutableMapping[containers.DaemonId, containers.Daemon],
) -> Collection[float]:
    """
    Terminate all daemons of an individual resource (gracefully and by force).

    All daemons are terminated in parallel to speed up the termination
    (especially taking into account that some daemons can take time to finish).

    The daemons are asked to terminate as soon as the object is marked
    for deletion. It can take some time until the deletion handlers also
    finish their work. The object is not physically deleted until all
    the daemons are terminated (by putting a finalizer on it).

    **Notes on this non-trivial implementation:**

    There is a same-purpose function `stop_daemon`, which works fully in-memory.
    That method is used when killing the daemons on operator exit.
    This method is used when the resource is deleted.

    The difference is that in this method (termination with delays and patches),
    other on-deletion handlers can be happening at the same time as the daemons
    are being terminated (it can take time due to backoffs and timeouts).
    In the end, the finalizer should be removed only once all deletion handlers
    have succeeded and all daemons are terminated -- not earlier than that.
    None of this (handlers and finalizers) is needed for the operator exiting.

    To know "when" the next check of daemons should be performed:

    * EITHER the operator should block this resource's processing and wait until
      the daemons are terminated -- thus leaking daemon's abstractions and logic
      and tools (e.g. aiojobs scheduler) to the upper level of processing;

    * OR the daemons termination should mimic the change-detection handlers
      and simulate the delays with multiple handling cycles -- in order to
      re-check the daemon's status regularly until they are done.

    Both of this approaches have the same complexity. But the latter one
    keep the logic isolated into the daemons module/routines (a bit cleaner).

    Hence, these duplicating methods of termination for different cases
    (as by their surrounding circumstances: deletion handlers and finalizers).
    """
    delays: List[float] = []
    now = time.monotonic()
    for daemon_id, daemon in daemons.items():
        logger = daemon.logger
        stopper = daemon.stopper
        age = (now - (stopper.when or now))

        if isinstance(daemon.handler, handlers_.ResourceDaemonHandler):
            backoff = daemon.handler.cancellation_backoff
            timeout = daemon.handler.cancellation_timeout
            polling = daemon.handler.cancellation_polling or DAEMON_POLLING_INTERVAL
        elif isinstance(daemon.handler, handlers_.ResourceTimerHandler):
            backoff = None
            timeout = None
            polling = DAEMON_POLLING_INTERVAL
        else:
            raise RuntimeError(f"Unsupported daemon handler: {daemon.handler!r}")

        # Whatever happens with other flags & logs & timings, this flag must be surely set.
        stopper.set(reason=primitives.DaemonStoppingReason.RESOURCE_DELETED)

        # It might be so, that the daemon exits instantly (if written properly). Avoid patching and
        # unnecessary handling cycles in this case: just give the asyncio event loop an extra cycle.
        await asyncio.sleep(0)

        # Try different approaches to exiting the daemon based on timings.
        if daemon.task.done():
            pass

        elif backoff is not None and age < backoff:
            if not stopper.is_set(reason=primitives.DaemonStoppingReason.DAEMON_SIGNALLED):
                stopper.set(reason=primitives.DaemonStoppingReason.DAEMON_SIGNALLED)
                logger.debug(f"Daemon {daemon_id!r} is signalled to exit gracefully.")
            delays.append(backoff - age)

        elif timeout is not None and age < timeout + (backoff or 0):
            if not stopper.is_set(reason=primitives.DaemonStoppingReason.DAEMON_CANCELLED):
                stopper.set(reason=primitives.DaemonStoppingReason.DAEMON_CANCELLED)
                logger.debug(f"Daemon {daemon_id!r} is signalled to exit by force.")
                daemon.task.cancel()
            delays.append(timeout + (backoff or 0) - age)

        elif timeout is not None:
            if not stopper.is_set(reason=primitives.DaemonStoppingReason.DAEMON_ABANDONED):
                stopper.set(reason=primitives.DaemonStoppingReason.DAEMON_ABANDONED)
                logger.warning(f"Daemon {daemon_id!r} did not exit in time. Leaving it orphaned.")
                warnings.warn(f"Daemon {daemon_id!r} did not exit in time.", ResourceWarning)

        else:
            logger.debug(f"Daemon {daemon_id!r} is still exiting. Next check is in {polling}s.")
            delays.append(polling)

    return delays


async def daemon_killer(
        *,
        settings: configuration.OperatorSettings,
        memories: containers.ResourceMemories,
) -> None:
    """
    An operator's root task to kill the daemons on the operator's shutdown.
    """

    # Sleep forever, or until cancelled, which happens when the operator begins its shutdown.
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass

    # Terminate all running daemons when the operator exits (and this task is cancelled).
    coros = [
        stop_daemon(daemon_id=daemon_id, daemon=daemon)
        for memory in memories.iter_all_memories()
        for daemon_id, daemon in memory.daemons.items()
    ]
    if coros:
        await asyncio.wait(coros)


async def stop_daemon(
        *,
        daemon_id: containers.DaemonId,
        daemon: containers.Daemon,
) -> None:
    """
    Stop a single daemon.

    The purpose is the same as in `stop_resource_daemons`, but this function
    is called on operator exiting, so there is no multi-step handling,
    everything happens in memory and linearly (while respecting the timing).

    For explanation on different implementations, see `stop_resource_daemons`.
    """
    if isinstance(daemon.handler, handlers_.ResourceDaemonHandler):
        backoff = daemon.handler.cancellation_backoff
        timeout = daemon.handler.cancellation_timeout
    elif isinstance(daemon.handler, handlers_.ResourceTimerHandler):
        backoff = None
        timeout = None
    else:
        raise RuntimeError(f"Unsupported daemon handler: {daemon.handler!r}")

    # Whatever happens with other flags & logs & timings, this flag must be surely set.
    # It might be so, that the daemon exits instantly (if written properly: give it chance).
    daemon.stopper.set(reason=primitives.DaemonStoppingReason.OPERATOR_EXITING)
    await asyncio.sleep(0)  # give a chance to exit gracefully if it can.
    if daemon.task.done():
        daemon.logger.debug(f"Daemon {daemon_id!r} has exited gracefully.")

    # Try different approaches to exiting the daemon based on timings.
    if not daemon.task.done() and backoff is not None:
        daemon.stopper.set(reason=primitives.DaemonStoppingReason.DAEMON_SIGNALLED)
        daemon.logger.debug(f"Daemon {daemon_id!r} is signalled to exit gracefully.")
        await asyncio.wait([daemon.task], timeout=backoff)

    if not daemon.task.done() and timeout is not None:
        daemon.stopper.set(reason=primitives.DaemonStoppingReason.DAEMON_CANCELLED)
        daemon.logger.debug(f"Daemon {daemon_id!r} is signalled to exit by force.")
        daemon.task.cancel()
        await asyncio.wait([daemon.task], timeout=timeout)

    if not daemon.task.done():
        daemon.stopper.set(reason=primitives.DaemonStoppingReason.DAEMON_ABANDONED)
        daemon.logger.warning(f"Daemon {daemon_id!r} did not exit in time. Leaving it orphaned.")
        warnings.warn(f"Daemon {daemon_id!r} did not exit in time.", ResourceWarning)


async def _runner(
        *,
        settings: configuration.OperatorSettings,
        handler: handlers_.ResourceSpawningHandler,
        memory: containers.ResourceMemory,
        cause: causation.DaemonCause,
) -> None:
    """
    Guard a running daemon during its life cycle.

    Note: synchronous daemons are awaited to the exit and postpone cancellation.
    The runner will not exit until the thread exits. See `invoke` for details.
    """
    try:
        if isinstance(handler, handlers_.ResourceDaemonHandler):
            await _resource_daemon(settings=settings, handler=handler, cause=cause)
        elif isinstance(handler, handlers_.ResourceTimerHandler):
            await _resource_timer(settings=settings, handler=handler, cause=cause, memory=memory)
        else:
            raise RuntimeError("Cannot determine which task wrapper to use. This is a bug.")

    finally:
        # Whatever happened, make sure the sync threads of asyncio threaded executor are notified:
        # in a hope that they will exit maybe some time later to free the OS/asyncio resources.
        # A possible case: operator is exiting and cancelling all "hung" non-root tasks, etc.
        cause.stopper.set(reason=primitives.DaemonStoppingReason.DONE)


async def _resource_daemon(
        *,
        settings: configuration.OperatorSettings,
        handler: handlers_.ResourceDaemonHandler,
        cause: causation.DaemonCause,
) -> None:
    """
    A long-running guarding task for a resource daemon handler.

    The handler is executed either once or repeatedly, based on the handler
    declaration.

    Few kinds of errors are suppressed, those expected from the daemons when
    they are cancelled due to the resource deletion.
    """
    logger = cause.logger

    if handler.initial_delay is not None:
        await sleeping.sleep_or_wait(handler.initial_delay, cause.stopper)

    # Similar to activities (in-memory execution), but applies patches on every attempt.
    state = states.State.from_scratch(handlers=[handler])
    while not cause.stopper.is_set() and not state.done:

        outcomes = await handling.execute_handlers_once(
            lifecycle=lifecycles.all_at_once,  # there is only one anyway
            settings=settings,
            handlers=[handler],
            cause=cause,
            state=state,
        )
        state = state.with_outcomes(outcomes)
        states.deliver_results(outcomes=outcomes, patch=cause.patch)

        if cause.patch:
            cause.logger.debug("Patching with: %r", cause.patch)
            await patching.patch_obj(resource=cause.resource, patch=cause.patch, body=cause.body)
            cause.patch.clear()

        # The in-memory sleep does not react to resource changes, but only to stopping.
        await sleeping.sleep_or_wait(state.delay, cause.stopper)

    if cause.stopper.is_set():
        logger.debug(f"{handler} has exited on request and will not be retried or restarted.")
    else:
        logger.debug(f"{handler} has exited on its own and will not be retried or restarted.")


async def _resource_timer(
        *,
        settings: configuration.OperatorSettings,
        handler: handlers_.ResourceTimerHandler,
        memory: containers.ResourceMemory,
        cause: causation.DaemonCause,
) -> None:
    """
    A long-running guarding task for resource timer handlers.

    Each individual handler for each individual k8s-object gets its own task.
    Despite asyncio can schedule the delayed execution of the callbacks
    with ``loop.call_later()`` and ``loop.call_at()``, we do not use them:

    * First, the callbacks are synchronous, making it impossible to patch
      the k8s-objects with the returned results of the handlers.

    * Second, our timers are more sophisticated: they track the last-seen time,
      obey the idle delays, and are instantly terminated/cancelled on the object
      deletion or on the operator exit.

    * Third, sharp timing would require an external timestamp storage anyway,
      which is easier to keep as a local variable inside of a function.

    It is hard to implement all of this with native asyncio timers.
    It is much easier to have an extra task which mostly sleeps,
    but calls the handling functions from time to time.
    """

    if handler.initial_delay is not None:
        await sleeping.sleep_or_wait(handler.initial_delay, cause.stopper)

    # Similar to activities (in-memory execution), but applies patches on every attempt.
    state = states.State.from_scratch(handlers=[handler])
    while not cause.stopper.is_set():  # NB: ignore state.done! it is checked below explicitly.

        # Reset success/failure retry counters & timers if it has succeeded. Keep it if failed.
        # Every next invocation of a successful handler starts the retries from scratch (from zero).
        if state.done:
            state = states.State.from_scratch(handlers=[handler])

        # Both `now` and `last_seen_time` are moving targets: the last seen time is updated
        # on every watch-event received, and prolongs the sleep. The sleep is never shortened.
        if handler.idle is not None:
            while not cause.stopper.is_set() and time.monotonic() - memory.idle_reset_time < handler.idle:
                delay = memory.idle_reset_time + handler.idle - time.monotonic()
                await sleeping.sleep_or_wait(delay, cause.stopper)
            if cause.stopper.is_set():
                continue

        # Remember the start time for the sharp timing and idle-time-waster below.
        started = time.monotonic()

        # Execute the handler as usually, in-memory, but handle its outcome on every attempt.
        outcomes = await handling.execute_handlers_once(
            lifecycle=lifecycles.all_at_once,  # there is only one anyway
            settings=settings,
            handlers=[handler],
            cause=cause,
            state=state,
        )
        state = state.with_outcomes(outcomes)
        states.deliver_results(outcomes=outcomes, patch=cause.patch)

        # Apply the accumulated patches after every invocation attempt (regardless of its outcome).
        if cause.patch:
            cause.logger.debug("Patching with: %r", cause.patch)
            await patching.patch_obj(resource=cause.resource, patch=cause.patch, body=cause.body)
            cause.patch.clear()

        # For temporary errors, override the schedule by the one provided by errors themselves.
        # It can be either a delay from TemporaryError, or a backoff for an arbitrary exception.
        if not state.done:
            await sleeping.sleep_or_wait(state.delays, cause.stopper)

        # For sharp timers, calculate how much time is left to fit the interval grid:
        #       |-----|-----|-----|-----|-----|-----|---> (interval=5, sharp=True)
        #       [slow_handler]....[slow_handler]....[slow...
        elif handler.interval is not None and handler.sharp:
            passed_duration = time.monotonic() - started
            remaining_delay = handler.interval - (passed_duration % handler.interval)
            await sleeping.sleep_or_wait(remaining_delay, cause.stopper)

        # For regular (non-sharp) timers, simply sleep from last exit to the next call:
        #       |-----|-----|-----|-----|-----|-----|---> (interval=5, sharp=False)
        #       [slow_handler].....[slow_handler].....[slow...
        elif handler.interval is not None:
            await sleeping.sleep_or_wait(handler.interval, cause.stopper)

        # For idle-only no-interval timers, wait till the next change (i.e. idling reset).
        # NB: This will skip the handler in the same tact (1/64th of a second) even if changed.
        elif handler.idle is not None:
            while memory.idle_reset_time <= started:
                await sleeping.sleep_or_wait(handler.idle, cause.stopper)

        # Only in case there are no intervals and idling, treat it as a one-shot handler.
        # This makes the handler practically meaningless, but technically possible.
        else:
            break
