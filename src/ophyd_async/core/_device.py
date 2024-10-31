from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine, Iterator, Mapping, MutableMapping
from logging import LoggerAdapter, getLogger
from typing import Any, TypeVar
from unittest.mock import Mock

from bluesky.protocols import HasName
from bluesky.run_engine import call_in_bluesky_event_loop, in_bluesky_event_loop

from ._protocol import Connectable
from ._utils import DEFAULT_TIMEOUT, NotConnected, wait_for_connection

_device_mocks: dict[Device, Mock] = {}


class DeviceConnector:
    """Defines how a `Device` should be connected and type hints processed."""

    def create_children_from_annotations(self, device: Device):
        """Used when children can be created from introspecting the hardware.

        Some control systems allow introspection of a device to determine what
        children it has. To allow this to work nicely with typing we add these
        hints to the Device like so::

            my_signal: SignalRW[int]
            my_device: MyDevice

        This method will be run during ``Device.__init__``, and is responsible
        for turning all of those type hints into real Signal and Device instances.

        Subsequent runs of this function should do nothing, to allow it to be
        called early in Devices that need to pass references to their children
        during ``__init__``.
        """

    async def connect(
        self,
        device: Device,
        mock: bool | Mock,
        timeout: float,
        force_reconnect: bool,
    ):
        """Used during ``Device.connect``.

        This is called when a previous connect has not been done, or has been
        done in a different mock more. It should connect the Device and all its
        children.
        """
        coros = {}
        for name, child_device in device.children():
            child_mock = getattr(mock, name) if mock else mock  # Mock() or False
            coros[name] = child_device.connect(
                mock=child_mock, timeout=timeout, force_reconnect=force_reconnect
            )
        await wait_for_connection(**coros)


class Device(HasName, Connectable):
    """Common base class for all Ophyd Async Devices."""

    _name: str = ""
    #: The parent Device if it exists
    parent: Device | None = None
    # None if connect hasn't started, a Task if it has
    _connect_task: asyncio.Task | None = None
    # If not None, then this is the mock arg of the previous connect
    # to let us know if we can reuse an existing connection
    _connect_mock_arg: bool | None = None

    def __init__(
        self, name: str = "", connector: DeviceConnector | None = None
    ) -> None:
        self._connector = connector or DeviceConnector()
        self.set_name(name)

    @property
    def name(self) -> str:
        """Return the name of the Device"""
        return self._name

    def children(self) -> Iterator[tuple[str, Device]]:
        for attr_name, attr in self.__dict__.items():
            if attr_name != "parent" and isinstance(attr, Device):
                yield attr_name, attr

    def set_name(self, name: str):
        """Set ``self.name=name`` and each ``self.child.name=name+"-child"``.

        Parameters
        ----------
        name:
            New name to set
        """
        self._name = name
        # Ensure self.log is recreated after a name change
        self.log = LoggerAdapter(
            getLogger("ophyd_async.devices"), {"ophyd_async_device_name": self.name}
        )
        for child_name, child in self.children():
            child_name = f"{self.name}-{child_name.strip('_')}" if self.name else ""
            child.set_name(child_name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "parent":
            if self.parent not in (value, None):
                raise TypeError(
                    f"Cannot set the parent of {self} to be {value}: "
                    f"it is already a child of {self.parent}"
                )
        elif isinstance(value, Device):
            value.parent = self
        return super().__setattr__(name, value)

    async def connect(
        self,
        mock: bool | Mock = False,
        timeout: float = DEFAULT_TIMEOUT,
        force_reconnect: bool = False,
    ) -> None:
        """Connect self and all child Devices.

        Contains a timeout that gets propagated to child.connect methods.

        Parameters
        ----------
        mock:
            If True then use ``MockSignalBackend`` for all Signals
        timeout:
            Time to wait before failing with a TimeoutError.
        """
        uses_mock = bool(mock)
        can_use_previous_connect = (
            uses_mock is self._connect_mock_arg
            and self._connect_task
            and not (self._connect_task.done() and self._connect_task.exception())
        )
        if mock is True:
            mock = Mock()  # create a new Mock if one not provided
        if force_reconnect or not can_use_previous_connect:
            self._connect_mock_arg = uses_mock
            if self._connect_mock_arg:
                _device_mocks[self] = mock
            coro = self._connector.connect(
                device=self, mock=mock, timeout=timeout, force_reconnect=force_reconnect
            )
            self._connect_task = asyncio.create_task(coro)

        assert self._connect_task, "Connect task not created, this shouldn't happen"
        # Wait for it to complete
        await self._connect_task


DeviceT = TypeVar("DeviceT", bound=Device)


class DeviceVector(MutableMapping[int, DeviceT], Device):
    """
    Defines device components with indices.

    In the below example, foos becomes a dictionary on the parent device
    at runtime, so parent.foos[2] returns a FooDevice. For example usage see
    :class:`~ophyd_async.epics.demo.DynamicSensorGroup`
    """

    def __init__(
        self,
        children: Mapping[int, DeviceT],
        name: str = "",
    ) -> None:
        self._children = dict(children)
        super().__init__(name=name)

    def __setattr__(self, name: str, child: Any) -> None:
        if name != "parent" and isinstance(child, Device):
            raise AttributeError(
                "DeviceVector can only have integer named children, "
                "set via device_vector[i] = child"
            )
        super().__setattr__(name, child)

    def __getitem__(self, key: int) -> DeviceT:
        return self._children[key]

    def __setitem__(self, key: int, value: DeviceT) -> None:
        # Check the types on entry to dict to make sure we can't accidentally
        # make a non-integer named child
        assert isinstance(key, int), f"Expected int, got {key}"
        assert isinstance(value, Device), f"Expected Device, got {value}"
        self._children[key] = value
        value.parent = self

    def __delitem__(self, key: int) -> None:
        del self._children[key]

    def __iter__(self) -> Iterator[int]:
        yield from self._children

    def __len__(self) -> int:
        return len(self._children)

    def children(self) -> Iterator[tuple[str, Device]]:
        for key, child in self._children.items():
            yield str(key), child

    def __hash__(self):  # to allow DeviceVector to be used as dict keys and in sets
        return hash(id(self))


class DeviceCollector:
    """Collector of top level Device instances to be used as a context manager

    Parameters
    ----------
    set_name:
        If True, call ``device.set_name(variable_name)`` on all collected
        Devices
    connect:
        If True, call ``device.connect(mock)`` in parallel on all
        collected Devices
    mock:
        If True, connect Signals in simulation mode
    timeout:
        How long to wait for connect before logging an exception

    Notes
    -----
    Example usage::

        [async] with DeviceCollector():
            t1x = motor.Motor("BLxxI-MO-TABLE-01:X")
            t1y = motor.Motor("pva://BLxxI-MO-TABLE-01:Y")
            # Names and connects devices here
        assert t1x.comm.velocity.source
        assert t1x.name == "t1x"

    """

    def __init__(
        self,
        set_name=True,
        connect=True,
        mock=False,
        timeout: float = 10.0,
    ):
        self._set_name = set_name
        self._connect = connect
        self._mock = mock
        self._timeout = timeout
        self._names_on_enter: set[str] = set()
        self._objects_on_exit: dict[str, Any] = {}

    def _caller_locals(self):
        """Walk up until we find a stack frame that doesn't have us as self"""
        try:
            raise ValueError
        except ValueError:
            _, _, tb = sys.exc_info()
            assert tb, "Can't get traceback, this shouldn't happen"
            caller_frame = tb.tb_frame
            while caller_frame.f_locals.get("self", None) is self:
                caller_frame = caller_frame.f_back
                assert (
                    caller_frame
                ), "No previous frame to the one with self in it, this shouldn't happen"
            return caller_frame.f_locals

    def __enter__(self) -> DeviceCollector:
        # Stash the names that were defined before we were called
        self._names_on_enter = set(self._caller_locals())
        return self

    async def __aenter__(self) -> DeviceCollector:
        return self.__enter__()

    async def _on_exit(self) -> None:
        # Name and kick off connect for devices
        connect_coroutines: dict[str, Coroutine] = {}
        for name, obj in self._objects_on_exit.items():
            if name not in self._names_on_enter and isinstance(obj, Device):
                if self._set_name and not obj.name:
                    obj.set_name(name)
                if self._connect:
                    connect_coroutines[name] = obj.connect(
                        self._mock, timeout=self._timeout
                    )

        # Connect to all the devices
        if connect_coroutines:
            await wait_for_connection(**connect_coroutines)

    async def __aexit__(self, type, value, traceback):
        self._objects_on_exit = self._caller_locals()
        await self._on_exit()

    def __exit__(self, type_, value, traceback):
        if in_bluesky_event_loop():
            raise RuntimeError(
                "Cannot use DeviceConnector inside a plan, instead use "
                "`yield from ophyd_async.plan_stubs.ensure_connected(device)`"
            )
        self._objects_on_exit = self._caller_locals()
        try:
            fut = call_in_bluesky_event_loop(self._on_exit())
        except RuntimeError as e:
            raise NotConnected(
                "Could not connect devices. Is the bluesky event loop running? See "
                "https://blueskyproject.io/ophyd-async/main/"
                "user/explanations/event-loop-choice.html for more info."
            ) from e
        return fut
