# Copyright (c) 2020 Huawei Technologies Co., Ltd
# Copyright (c) 2019, Facebook CORPORATION. 
# All rights reserved.
#
# Licensed under the BSD 3-Clause License  (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://opensource.org/licenses/BSD-3-Clause
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import logging
import time
import warnings
from datetime import timedelta
from typing import Dict, Optional, Tuple, Union

import torch
from torch._six import string_classes
from torch.distributed.constants import default_pg_timeout
from torch.distributed.rendezvous import rendezvous  # noqa: F401
from torch._C._distributed_c10d import (
    AllreduceOptions,
    AllreduceCoalescedOptions,
    AllToAllOptions,
    BarrierOptions,
    BroadcastOptions,
    GatherOptions,
    PrefixStore,
    ProcessGroup,
    ReduceOptions,
    ReduceOp,
    ReduceScatterOptions,
    ScatterOptions,
    Store,
)

_MPI_AVAILABLE = True
_NCCL_AVAILABLE = True
_GLOO_AVAILABLE = True
_HCCL_AVAILABLE = True

try:
    from torch._C._distributed_c10d import ProcessGroupMPI
except ImportError:
    _MPI_AVAILABLE = False

try:
    from torch._C._distributed_c10d import ProcessGroupNCCL
except ImportError:
    _NCCL_AVAILABLE = False

try:
    from torch._C._distributed_c10d import ProcessGroupGloo
except ImportError:
    _GLOO_AVAILABLE = False

try:
    from torch_npu._C._distributed_c10d import ProcessGroupHCCL
except ImportError:
    _HCCL_AVAILABLE = False

__all__ = [
    "Backend", "_backend", "Group", "GroupMember", "is_hccl_available", "is_initialized",
    "get_backend", "init_process_group", "destroy_process_group", "get_rank", "get_world_size",
    "isend", "irecv", "send", "recv", "P2POp", "batch_isend_irecv", "broadcast", "all_reduce",
    "all_reduce_coalesced", "reduce", "all_gather", "all_gather_coalesced", "gather", "scatter",
    "reduce_scatter", "all_to_all_single", "all_to_all", "barrier", "new_group", "ProcessGroupHCCL",
    "_get_default_group", "_get_global_rank"
]

# Some reduce ops are not supported by complex numbers and will result in an error.
# We currently provide complex support to the distributed API by viewing
# complex tensors as real (torch.view_as_real), meaning that calling
# these unsupported ops will return garbage values rather than error out.
# (e.g. max(2+3i, 3+2i) = 3+3i)
# We'd like calls to unsupported ops to error out accordingly,
# rather than returning garbage values.
def supports_complex(reduceOp: ReduceOp) -> bool:
    denyList = [ReduceOp.MAX, ReduceOp.MIN, ReduceOp.PRODUCT,
                ReduceOp.BAND, ReduceOp.BOR, ReduceOp.BXOR]
    return reduceOp not in denyList


class Backend(object):
    """
    An enum-like class of available backends: GLOO, NCCL, MPI, and other registered
    backends.

    The values of this class are lowercase strings, e.g., ``"gloo"``. They can
    be accessed as attributes, e.g., ``Backend.NCCL``.

    This class can be directly called to parse the string, e.g.,
    ``Backend(backend_str)`` will check if ``backend_str`` is valid, and
    return the parsed lowercase string if so. It also accepts uppercase strings,
    e.g., ``Backend("GLOO")`` returns ``"gloo"``.

    .. note:: The entry ``Backend.UNDEFINED`` is present but only used as
              initial value of some fields. Users should neither use it directly
              nor assume its existence.
    """
    UNDEFINED = "undefined"
    GLOO = "gloo"
    NCCL = "nccl"
    MPI = "mpi"
    TCP = "tcp"
    HCCL = "hccl"

    def __new__(cls, name: str):
        if not isinstance(name, string_classes):
            raise ValueError("Backend name must be a string, but got: {}".format(name))
        value = getattr(Backend, name.upper(), Backend.UNDEFINED)

        if value == Backend.TCP:
            raise ValueError("TCP backend has been deprecated. Please use "
                             "Gloo or MPI backend for collective operations "
                             "on CPU tensors.")
        elif value == Backend.UNDEFINED:
            raise ValueError("Invalid backend: '{}'".format(name))
        elif value != Backend.GLOO and value != Backend.NCCL and value != Backend.MPI:
            value = name
        return value

    @classmethod
    def register_backend(cls, name, func):
        """
        Registers a new backend.

        This class method is used by 3rd party cpp extension to register new backend.

        Args:
            name (str): Backend name matching with the one in `init_process_group()`.
            func (function): Function handler that instantiates the backend.
                             The function should be implemented in the backend cpp extension
                             and takes four arguments, including prefix_store, rank,
                             world_size, and timeout.

        .. note:: This support of 3rd party backend is experimental and subject to change.

        """
        setattr(Backend, name.upper(), func)

# `_backend`, `dist_backend`, and `reduce_op` are here to maintain backward
# compatibility with pre-c10d distributed package.
# TODO: remove them when users are ready to take a hard dependency on PyTorch 1.
_backend: str = Backend.UNDEFINED
dist_backend = Backend


class _reduce_op(object):
    r"""
    Deprecated enum-like class for reduction operations: ``SUM``, ``PRODUCT``,
    ``MIN``, and ``MAX``.

    :class:`~torch.distributed.ReduceOp` is recommended to use instead.
    """

    def __init__(self):
        # __members__ is a dict storing key-value pairs for enum classes
        for k, v in ReduceOp.__members__.items():
            setattr(self, k, v)
        self.__members__ = ReduceOp.__members__

    def __getattribute__(self, key):
        warnings.warn("torch.distributed.reduce_op is deprecated, please use "
                      "torch.distributed.ReduceOp instead")
        return object.__getattribute__(self, key)

reduce_op = _reduce_op()


class Group(object):
    # Points to the default PG once initialized.
    WORLD: Optional[ProcessGroup] = None


class GroupMember(object):
    # Alias to Group.WORLD for backward compatibility
    WORLD = Group.WORLD
    NON_GROUP_MEMBER = object()


# Cached process groups
# For NCCL and GLOO pg, it is a map from ProcessGroup to (Backend, Store)
# For MPI pg, it is a map from ProcessGroup to (Backend, None)
_pg_map: Dict[ProcessGroup, Tuple[str, Optional[Store]]] = {}
# Process group's names, map from ProcessGroup to str
_pg_names: Dict[ProcessGroup, str] = {}
# Process group's global rank to local rank mapping
_pg_group_ranks: Dict[ProcessGroup, Dict[int, int]] = {}

# Default process group state
_default_pg_init_method = None

# Process group count for default naming
_group_count = 0

STORE_BASED_BARRIER_PREFIX = "store_based_barrier_key"

def _store_based_barrier(rank, store, timeout):
    """
    Barrier based on store which is used for synchronizing processes after
    ``init_process_group`` or ``new_group``. Intended to be used only with
    those two methods and is not a generic alternative to ``barrier()``.
    """
    store_key = "{}:{}".format(STORE_BASED_BARRIER_PREFIX, _group_count)
    store.add(store_key, 1)
    logging.info('Added key: {} to store for rank: {}'.format(store_key, rank))

    # Now wait for all workers to check in with the store.
    world_size = get_world_size()
    # Use 'add' instead of 'get' since for some store implementations 'add'
    # doesn't work well with 'get'. Ideally the store implementations should
    # be fixed, but for backward compatiblity reasons it is risky to change
    # the store implementations. Once, we completely migrate away from these
    # legacy stores, we can use 'get' here instead.
    worker_count = store.add(store_key, 0)
    start = time.time()
    log_time = time.time()
    while worker_count != world_size:
        time.sleep(0.01)
        worker_count = store.add(store_key, 0)

        # Print status periodically to keep track.
        if timedelta(seconds=(time.time() - log_time)) > timedelta(seconds=10):
            logging.info(
                "Waiting in store based barrier to initialize process group for "
                "rank: {}, key: {} (world_size={}, worker_count={}, timeout={})".format(
                    rank, store_key, world_size, worker_count, timeout))
            log_time = time.time()

        if timedelta(seconds=(time.time() - start)) > timeout:
            raise RuntimeError(
                "Timed out initializing process group in store based barrier on "
                "rank: {}, for key: {} (world_size={}, worker_count={}, timeout={})".format(
                    rank, store_key, world_size, worker_count, timeout))

def _rank_not_in_group(group: ProcessGroup):
    """
    Helper that checks if the current process's rank is not in a given group.
    """
    if group is None:
        return False
    return group == GroupMember.NON_GROUP_MEMBER


def _get_group_rank(group: ProcessGroup, rank):
    """
    Helper that gets a given group's local rank in the group from a given global
    rank.
    """
    if group is GroupMember.WORLD:
        raise RuntimeError("Group.WORLD does not have local rank to global "
                           "rank mapping")
    if group not in _pg_group_ranks:
        raise RuntimeError("The given group does not exist")
    try:
        group_rank = _pg_group_ranks[group][rank]
    except KeyError:
        raise RuntimeError(f"The global rank {rank} is not part of the group {group}") from None
    return group_rank


def _get_global_rank(group, group_rank):
    """
    Helper that gets a given group's global rank from a given local rank in the
    group.
    """
    if group is GroupMember.WORLD:
        raise RuntimeError("Group.WORLD does not have local rank to global "
                           "rank mapping")
    group_rank_map = _pg_group_ranks[group]
    for rank, grp_rank in group_rank_map.items():
        if grp_rank == group_rank:
            return rank
    raise RuntimeError("The group rank is not part of the group")


def _get_group_size(group):
    """
    Helper that gets a given group's world size.
    """
    if group is GroupMember.WORLD or group is None:
        default_pg = _get_default_group()
        return default_pg.size()
    if group not in _pg_group_ranks:
        raise RuntimeError("The given group does not exist")
    return len(_pg_group_ranks[group])


def _check_single_tensor(param, param_name):
    """
    Helper to check that the parameter ``param_name`` is a single tensor.
    """
    if not isinstance(param, torch.Tensor):
        raise RuntimeError("Invalid function argument. Expected parameter `{}` "
                           "to be of type torch.Tensor.".format(param_name))


def _check_tensor_list(param, param_name):
    """
    Helper to check that the parameter ``param_name`` is a list of tensors.
    """
    if not isinstance(param, list) or \
       not all(isinstance(p, torch.Tensor) for p in param):
        raise RuntimeError("Invalid function argument. Expected parameter `{}` "
                           "to be of type List[torch.Tensor].".format(param_name))


def _check_op(op):
    """
    Helper to check that the ``op`` is either isend or irecv.
    """
    if op not in [isend, irecv]:
        raise RuntimeError("Invalid ``op``. Expected ``op`` "
                           "to be of type ``torch.distributed.isend`` or "
                           "``torch.distributed.irecv``.")

def _check_p2p_op_list(p2p_op_list):
    """
    Helper to check that the ``p2p_op_list`` is a list of P2POp instances and
    all ops use the same backend.
    """
    if not isinstance(p2p_op_list, list) or \
       not all(isinstance(p2p_op, P2POp) for p2p_op in p2p_op_list):
        raise RuntimeError("Invalid ``p2p_op_list``. Each op is expected to "
                           "to be of type ``torch.distributed.P2POp``.")


    backend = get_backend(p2p_op_list[0].group)
    if not all(backend == get_backend(p2p_op.group) for p2p_op in p2p_op_list):
        raise RuntimeError("All groups need to use the same backend.")


def is_mpi_available():
    """
    Checks if the MPI backend is available.
    """
    return _MPI_AVAILABLE


def is_nccl_available():
    """
    Checks if the NCCL backend is available.
    """
    return _NCCL_AVAILABLE


def is_gloo_available():
    """
    Checks if the Gloo backend is available.
    """
    return _GLOO_AVAILABLE

def is_hccl_available():
    """
    Checks if the HCCL backend is available.

    """
    return _HCCL_AVAILABLE

def is_initialized():
    """
    Checking if the default process group has been initialized
    """
    return GroupMember.WORLD is not None


def _get_default_group():
    """
    Getting the default process group created by init_process_group
    """
    if not is_initialized():
        raise RuntimeError("Default process group has not been initialized, "
                           "please make sure to call init_process_group.")
    return GroupMember.WORLD


def _get_default_store():
    """
    Getting the default store created by init_process_group
    """
    if not is_initialized():
        raise RuntimeError("Default process group has not been initialized, "
                           "please make sure to call init_process_group.")
    default_pg = _get_default_group()
    _, default_store = _pg_map[default_pg]
    return default_store

def _update_default_pg(pg):
    GroupMember.WORLD = Group.WORLD = pg


def get_backend(group=None):
    """
    Returns the backend of the given process group.

    Args:
        group (ProcessGroup, optional): The process group to work on. The
            default is the general main process group. If another specific group
            is specified, the calling process must be part of :attr:`group`.

    Returns:
        The backend of the given process group as a lower case string.

    """
    if group is None:
        pg = _get_default_group()
    else:
        pg = group
    if _rank_not_in_group(pg):
        raise RuntimeError("Invalid process group specified")
    pg_store = _pg_map.get(pg, None)
    assert pg_store is not None
    return pg_store[0]


def init_process_group(backend, init_method=None, timeout=default_pg_timeout,
                       world_size=-1, rank=-1, store=None, group_name=''):
    """
    Initializes the default distributed process group, and this will also
    initialize the distributed package.

    There are 2 main ways to initialize a process group:
        1. Specify ``store``, ``rank``, and ``world_size`` explicitly.
        2. Specify ``init_method`` (a URL string) which indicates where/how
           to discover peers. Optionally specify ``rank`` and ``world_size``,
           or encode all required parameters in the URL and omit them.

    If neither is specified, ``init_method`` is assumed to be "env://".


    Args:
        backend (str or Backend): The backend to use. Depending on
            build-time configurations, valid values include ``mpi``, ``gloo``,
            and ``nccl``. This field should be given as a lowercase string
            (e.g., ``"gloo"``), which can also be accessed via
            :class:`Backend` attributes (e.g., ``Backend.GLOO``). If using
            multiple processes per machine with ``nccl`` backend, each process
            must have exclusive access to every GPU it uses, as sharing GPUs
            between processes can result in deadlocks.
        init_method (str, optional): URL specifying how to initialize the
                                     process group. Default is "env://" if no
                                     ``init_method`` or ``store`` is specified.
                                     Mutually exclusive with ``store``.
        world_size (int, optional): Number of processes participating in
                                    the job. Required if ``store`` is specified.
        rank (int, optional): Rank of the current process (it should be a
                              number between 0 and ``world_size``-1).
                              Required if ``store`` is specified.
        store(Store, optional): Key/value store accessible to all workers, used
                                to exchange connection/address information.
                                Mutually exclusive with ``init_method``.
        timeout (timedelta, optional): Timeout for operations executed against
            the process group. Default value equals 30 minutes.
            This is applicable for the ``gloo`` backend. For ``nccl``, this is
            applicable only if the environment variable ``NCCL_BLOCKING_WAIT``
            or ``NCCL_ASYNC_ERROR_HANDLING`` is set to 1. When
            ``NCCL_BLOCKING_WAIT`` is set, this is the duration for which the
            process will block and wait for collectives to complete before
            throwing an exception. When ``NCCL_ASYNC_ERROR_HANDLING`` is set,
            this is the duration after which collectives will be aborted
            asynchronously and the process will crash. ``NCCL_BLOCKING_WAIT``
            will provide errors to the user which can be caught and handled,
            but due to its blocking nature, it has a performance overhead. On
            the other hand, ``NCCL_ASYNC_ERROR_HANDLING`` has very little
            performance overhead, but crashes the process on errors. This is
            done since CUDA execution is async and it is no longer safe to
            continue executing user code since failed async NCCL operations
            might result in subsequent CUDA operations running on corrupted
            data. Only one of these two environment variables should be set.
        group_name (str, optional, deprecated): Group name.

    To enable ``backend == Backend.MPI``, PyTorch needs to be built from source
    on a system that supports MPI.

    """
    global _pg_group_ranks
    global _backend
    global _default_pg_init_method

    if not isinstance(timeout, timedelta):
        raise RuntimeError("Expected timeout argument to be of type"
                           "datetime.timedelta")

    if GroupMember.WORLD is not None:
        raise RuntimeError("trying to initialize the default process group "
                           "twice!")

    assert (store is None) or (init_method is None), \
        "Cannot specify both init_method and store."

    if store is not None:
        assert world_size > 0, 'world_size must be positive if using store'
        assert rank >= 0, 'rank must be non-negative if using store'
    elif init_method is None:
        init_method = "env://"

    backend = Backend(backend)

    if backend == Backend.MPI:
        if world_size != -1 or rank != -1:
            warnings.warn(
                "For MPI backend, world_size ({}) and rank ({}) "
                "are ignored since they are assigned by the "
                "MPI runtime.".format(world_size, rank))

        _update_default_pg(_new_process_group_helper(-1, -1, [], Backend.MPI, None, group_name=group_name,
                                                     timeout=timeout))
    else:
        # backward compatible API
        if store is None:
            rendezvous_iterator = rendezvous(
                init_method, rank, world_size, timeout=timeout
            )
            store, rank, world_size = next(rendezvous_iterator)
            store.set_timeout(timeout)

        _update_default_pg(_new_process_group_helper(world_size, rank, [], backend, store,
                                                     group_name=group_name, timeout=timeout))

    _pg_group_ranks[GroupMember.WORLD] = {i: i for i in range(GroupMember.WORLD.size())}  # type: ignore
    _backend = _pg_map[GroupMember.WORLD][0]  # type: ignore
    _default_pg_init_method = init_method

    # barrier at the end to ensure that once we return from this method, all
    # process groups including global variables are updated correctly on all
    # ranks.
    if backend == Backend.MPI:
        # MPI backend doesn't use store.
        barrier()
    else:
        # Use store based barrier here since barrier() used a bunch of
        # default devices and messes up NCCL internal state.
        _store_based_barrier(rank, store, timeout)


def _new_process_group_helper(world_size, rank, group_ranks, backend, store, group_name=None,
                              timeout=default_pg_timeout):
    """
    Create a new distributed process group.

    This function must be called by ALL processes in the global group, even if
    the calling process is not part of the newly created group. In that case,
    this function returns GroupMember.NON_GROUP_MEMBER.

    This function is called with ``group_ranks == []`` for the default group.
    """
    global _pg_map
    global _group_count
    global _pg_names

    if not group_name:
        group_name = str(_group_count)
        _group_count += 1

    if group_name in _pg_names.values():
        raise RuntimeError("The specified group name has already been created, please use a different group name")

    if not isinstance(timeout, timedelta):
        raise RuntimeError("Expected timeout argument to be of type datetime.timedelta")

    # The list of group ranks is empty if we're creating the default group.
    is_default_group = (len(group_ranks) == 0)

    backend = Backend(backend)
    pg: Union[ProcessGroupGloo, ProcessGroupMPI, ProcessGroupNCCL, ProcessGroupHCCL]
    if backend == Backend.MPI:
        if not is_mpi_available():
            raise RuntimeError("Distributed package doesn't have MPI built in."
                               " MPI is only included if you build PyTorch from"
                               " source on a host that has MPI installed.")
        pg = ProcessGroupMPI.create(group_ranks)
        if not pg:
            return GroupMember.NON_GROUP_MEMBER
        _pg_map[pg] = (Backend.MPI, None)
        _pg_names[pg] = group_name
    else:
        # If this is a subgroup (which means group_ranks is specified),
        # we check if the current process is a member of the new group.
        if not is_default_group:
            global_rank = _get_default_group().rank()
            if global_rank not in group_ranks:
                return GroupMember.NON_GROUP_MEMBER

        # Use the group name as prefix in the default store, such that
        # a single store can be reused by multiple groups.
        prefix_store = PrefixStore(group_name, store)

        if backend == Backend.GLOO:
            pg = ProcessGroupGloo(prefix_store, rank, world_size, timeout=timeout)
            _pg_map[pg] = (Backend.GLOO, store)
            _pg_names[pg] = group_name
        elif backend == Backend.NCCL:
            if not is_nccl_available():
                raise RuntimeError("Distributed package doesn't have NCCL built in")
            pg = ProcessGroupNCCL(prefix_store, rank, world_size, timeout)
            _pg_map[pg] = (Backend.NCCL, store)
            _pg_names[pg] = group_name
        elif backend == Backend.HCCL:
            if not is_hccl_available():
                raise RuntimeError("Distributed package doesn't have HCCL built in")
            pg = ProcessGroupHCCL(prefix_store, rank, world_size)
            _pg_map[pg] = (Backend.HCCL, store)
            _pg_names[pg] = group_name
        else:
            pg = getattr(Backend, backend.upper())(prefix_store, rank, world_size, timeout)
            _pg_map[pg] = (backend, store)
            _pg_names[pg] = group_name

    return pg


def destroy_process_group(group=None):
    """
    Destroy a given process group, and deinitialize the distributed package

    Args:
        group (ProcessGroup, optional): The process group to be destroyed, if
                                        Group.WORLD is given, all process
                                        groups including the default one will
                                        be destroyed.
    """
    global _pg_map
    global _pg_names
    global _pg_group_ranks
    global _default_pg_init_method
    global _group_count

    if group == GroupMember.NON_GROUP_MEMBER:
        return

    if group is None:
        pg = GroupMember.WORLD
    else:
        pg = group

    assert pg is not None
    if _pg_map.get(pg, None) is None:
        raise RuntimeError("Invalid process group specified")

    if group is None or group == GroupMember.WORLD:
        _update_default_pg(None)
        _default_pg_init_method = None
        _pg_map.clear()
        _pg_names.clear()
        _pg_group_ranks.clear()

        # when process group doesn't have an explicit name (only WORLD (default)
        # process group can have an explicit name), we use global _group_counter
        # to generate the name. We need to reset the counter on destruction to
        # allow consistent value to be generated when we re-create process
        # groups after some trainers recover from failure
        #
        # We only reset this when WORLD is being destroyed because if this
        # process group is in good state, we aren't dealing with failures.
        _group_count = 0
    else:
        del _pg_map[pg]
        del _pg_names[pg]
        del _pg_group_ranks[pg]


def get_rank(group=None):
    """
    Returns the rank of current process group

    Rank is a unique identifier assigned to each process within a distributed
    process group. They are always consecutive integers ranging from 0 to
    ``world_size``.

    Args:
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.

    Returns:
        The rank of the process group
        -1, if not part of the group

    """
    if _rank_not_in_group(group):
        return -1

    default_pg = _get_default_group()
    if group is None or group is GroupMember.WORLD:
        return default_pg.rank()

    return _get_group_rank(group, default_pg.rank())


def get_world_size(group=None):
    """
    Returns the number of processes in the current process group

    Args:
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.

    Returns:
        The world size of the process group
        -1, if not part of the group

    """
    if _rank_not_in_group(group):
        return -1

    return _get_group_size(group)


def isend(tensor,
          dst,
          group=None,
          tag=0):
    """
    Sends a tensor asynchronously.

    Args:
        tensor (Tensor): Tensor to send.
        dst (int): Destination rank.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        tag (int, optional): Tag to match send with remote recv

    Returns:
        A distributed request object.
        None, if not part of the group

    """
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    if group is None or group is GroupMember.WORLD:
        default_pg = _get_default_group()
        return default_pg.send([tensor], dst, tag)
    else:
        group_dst_rank = _get_group_rank(group, dst)
        return group.send([tensor], group_dst_rank, tag)


def irecv(tensor,
          src=None,
          group=None,
          tag=0):
    """
    Receives a tensor asynchronously.

    Args:
        tensor (Tensor): Tensor to fill with received data.
        src (int, optional): Source rank. Will receive from any
            process if unspecified.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        tag (int, optional): Tag to match recv with remote send

    Returns:
        A distributed request object.
        None, if not part of the group

    """
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    if group is None or group is GroupMember.WORLD:
        pg = _get_default_group()
    else:
        pg = group

    if src is None:
        return pg.recv_anysource([tensor], tag)
    else:
        if pg is GroupMember.WORLD:
            return pg.recv([tensor], src, tag)
        else:
            group_src_rank = _get_group_rank(pg, src)
            return pg.recv([tensor], group_src_rank, tag)


def send(tensor,
         dst,
         group=None,
         tag=0):
    """
    Sends a tensor synchronously.

    Args:
        tensor (Tensor): Tensor to send.
        dst (int): Destination rank.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        tag (int, optional): Tag to match send with remote recv

    """
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    if group is None or group is GroupMember.WORLD:
        default_pg = _get_default_group()
        default_pg.send([tensor], dst, tag).wait()
    else:
        group_dst_rank = _get_group_rank(group, dst)
        group.send([tensor], group_dst_rank, tag).wait()


def recv(tensor,
         src=None,
         group=None,
         tag=0):
    """
    Receives a tensor synchronously.

    Args:
        tensor (Tensor): Tensor to fill with received data.
        src (int, optional): Source rank. Will receive from any
            process if unspecified.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        tag (int, optional): Tag to match recv with remote send

    Returns:
        Sender rank
        -1, if not part of the group

    """
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return -1

    if group is None:
        pg = _get_default_group()
    else:
        pg = group

    if src is None:
        work = pg.recv_anysource([tensor], tag)
        work.wait()
        src_rank = work._source_rank()
        if group is None or group is GroupMember.WORLD:
            return src_rank
        else:
            return _get_global_rank(pg, src_rank)
    else:
        if group is None or group is GroupMember.WORLD:
            pg.recv([tensor], src, tag).wait()
        else:
            group_src_rank = _get_group_rank(pg, src)
            pg.recv([tensor], group_src_rank, tag).wait()
        return src


class P2POp(object):
    """
    A class to build point-to-point operations for ``batch_isend_irecv``.

    This class builds the type of P2P operation, communication buffer, peer rank,
    Process Group group, and tag. Instances of this class will be passed to
    ``batch_isend_irecv`` for point-to-point communications.

    Args:
        op (callable): A function to send data to or receive data from a peer process.
            The type of ``op`` is either ``torch.distributed.isend`` or
            ``torch.distributed.irecv``.
        tensor (Tensor): Tensor to send or receive.
        peer (int): Destination or source rank.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        tag (int, optional): Tag to match send with recv.
    """
    def __init__(self, op, tensor, peer, group=None, tag=0):
        self.op = op
        self.tensor = tensor
        self.peer = peer
        self.group = group
        self.tag = tag

    def __new__(cls, op, tensor, peer, group=None, tag=0):
        _check_op(op)
        _check_single_tensor(tensor, "tensor")
        return object.__new__(cls)


@contextlib.contextmanager
def _batch_p2p_manager(backend):
    if backend == Backend.NCCL:
        ProcessGroupNCCL._group_start()
    try:
        yield
    finally:
        if backend == Backend.NCCL:
            ProcessGroupNCCL._group_end()


def batch_isend_irecv(p2p_op_list):
    """
    Send or Receive a batch of tensors asynchronously and return a list of requests.

    Process each of the operations in p2p_op_list and return the corresponding
    requests. NCCL and Gloo backend are currently supported.

    Args:
        p2p_op_list: A list of point-to-point operations(type of each operator is
            ``torch.distributed.P2POp``). The order of the isend/irecv in the list
            matters and it needs to match with corresponding isend/irecv on the
            remote end.

    Returns:
        A list of distributed request objects returned by calling the corresponding
        op in the op_list.

    Examples:
        >>> send_tensor = torch.arange(2) + 2 * rank
        >>> recv_tensor = torch.randn(2)
        >>> send_op = dist.P2POp(dist.isend, send_tensor, (rank + 1)%world_size)
        >>> recv_op = dist.P2POp(dist.irecv, recv_tensor, (rank + 1)%world_size)
        >>> reqs = batch_isend_irecv([send_op, recv_op])
        >>> for req in reqs:
        >>>     req.wait()
        >>> recv_tensor
        tensor([2, 3])     # Rank 0
        tensor([0, 1])     # Rank 1

    .. note:: Note that when this API is used with the NCCL PG backend, users must set
        the current GPU device with `torch.cuda.set_device`, otherwise it will
        lead to unexpected hang issues.
    """
    _check_p2p_op_list(p2p_op_list)
    backend = get_backend(p2p_op_list[0].group)
    reqs = []
    with _batch_p2p_manager(backend):
        for p2p_op in p2p_op_list:
            op = p2p_op.op
            tensor = p2p_op.tensor
            peer = p2p_op.peer
            curr_group = p2p_op.group
            tag = p2p_op.tag

            ret = op(tensor, peer, curr_group, tag)

            if ret is not None:
                reqs.append(ret)
    return reqs


def broadcast(tensor,
              src,
              group=None,
              async_op=False):
    """
    Broadcasts the tensor to the whole group.

    ``tensor`` must have the same number of elements in all processes
    participating in the collective.

    Args:
        tensor (Tensor): Data to be sent if ``src`` is the rank of current
            process, and tensor to be used to save received data otherwise.
        src (int): Source rank.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    """
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    opts = BroadcastOptions()
    opts.rootRank = src
    opts.rootTensor = 0

    if group is None or group is GroupMember.WORLD:
        default_pg = _get_default_group()
        work = default_pg.broadcast([tensor], opts)
    else:
        group_src_rank = _get_group_rank(group, src)
        opts.rootRank = group_src_rank
        work = group.broadcast([tensor], opts)
    if async_op:
        return work
    else:
        work.wait()


def all_reduce(tensor,
               op=ReduceOp.SUM,
               group=None,
               async_op=False):
    """
    Reduces the tensor data across all machines in such a way that all get
    the final result.

    After the call ``tensor`` is going to be bitwise identical in all processes.

    Complex tensors are supported.

    Args:
        tensor (Tensor): Input and output of the collective. The function
            operates in-place.
        op (optional): One of the values from
            ``torch.distributed.ReduceOp``
            enum.  Specifies an operation used for element-wise reductions.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    Examples:
        >>> # All tensors below are of torch.int64 type.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor = torch.arange(2, dtype=torch.int64) + 1 + 2 * rank
        >>> tensor
        tensor([1, 2]) # Rank 0
        tensor([3, 4]) # Rank 1
        >>> dist.all_reduce(tensor, op=ReduceOp.SUM)
        >>> tensor
        tensor([4, 6]) # Rank 0
        tensor([4, 6]) # Rank 1

        >>> # All tensors below are of torch.cfloat type.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor = torch.tensor([1+1j, 2+2j], dtype=torch.cfloat) + 2 * rank * (1+1j)
        >>> tensor
        tensor([1.+1.j, 2.+2.j]) # Rank 0
        tensor([3.+3.j, 4.+4.j]) # Rank 1
        >>> dist.all_reduce(tensor, op=ReduceOp.SUM)
        >>> tensor
        tensor([4.+4.j, 6.+6.j]) # Rank 0
        tensor([4.+4.j, 6.+6.j]) # Rank 1

    """
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    if tensor.is_complex():
        if not supports_complex(op):
            raise RuntimeError(f"all_reduce does not support {op} on complex tensors")
        tensor = torch.view_as_real(tensor)

    opts = AllreduceOptions()
    opts.reduceOp = op
    if group is None:
        default_pg = _get_default_group()
        work = default_pg.allreduce([tensor], opts)
    else:
        work = group.allreduce([tensor], opts)

    if async_op:
        return work
    else:
        work.wait()


def all_reduce_coalesced(tensors,
                         op=ReduceOp.SUM,
                         group=None,
                         async_op=False):
    """
    WARNING: at this time individual shape checking is not implemented across nodes.
    For example, if the rank 0 node passes [torch.rand(4), torch.rand(2)] and the
    rank 1 node passes [torch.rand(2), torch.rand(2), torch.rand(2)], the allreduce
    operation will proceed without complaint and return erroneous outputs. This lack
    of shape checking results in significant performance improvements but users of this
    function should take extra care to ensure that each node passes in tensors whose
    shapes match across nodes.

    Reduces each tensor in tensors (residing on the same device) across all machines
    in such a way that all get the final result.

    After the call each tensor in tensors is going to bitwise identical
    in all processes.

    Complex tensors are supported.

    Args:
        tensors (List[Tensor]): Input and output of the collective. The function
            operates in-place.
        op (Optional[ReduceOp]): One of the values from
            ``torch.distributed.ReduceOp`` enum. Specifies an operation used for
            element-wise reductions.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (Optional[bool]): Whether this op should be an async op.

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group.

    """
    _check_tensor_list(tensors, "tensor")
    if _rank_not_in_group(group):
        return

    if any([t.is_complex() for t in tensors]) and not supports_complex(op):
        raise RuntimeError(f"all_reduce does not support {op} on complex tensors")

    tensors = [t if not t.is_complex() else torch.view_as_real(t) for t in tensors]

    opts = AllreduceCoalescedOptions()
    opts.reduceOp = op
    if group is None:
        default_pg = _get_default_group()
        work = default_pg.allreduce_coalesced(tensors, opts)
    else:
        work = group.allreduce_coalesced(tensors, opts)

    if async_op:
        return work
    else:
        work.wait()


def reduce(tensor,
           dst,
           op=ReduceOp.SUM,
           group=None,
           async_op=False):
    """
    Reduces the tensor data across all machines.

    Only the process with rank ``dst`` is going to receive the final result.

    Args:
        tensor (Tensor): Input and output of the collective. The function
            operates in-place.
        dst (int): Destination rank
        op (optional): One of the values from
            ``torch.distributed.ReduceOp``
            enum.  Specifies an operation used for element-wise reductions.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    """
    if async_op:
        raise RuntimeError("Reduce implemented by all_reduce: "
                           "not support async")
        return

    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    opts = AllreduceOptions()
    opts.reduceOp = op

    if group is None or group is GroupMember.WORLD:
        default_pg = _get_default_group()
        group_dst_rank = dst
        current_rank = get_rank()
        tensor_tmp = tensor.clone()
        work = default_pg.allreduce([tensor_tmp], opts)
    else:
        group_dst_rank = _get_group_rank(group, dst)
        current_rank = get_rank(group)
        tensor_tmp = tensor.clone()
        work = group.allreduce([tensor_tmp], opts)

    if async_op:
        return work
    else:
        work.wait()
        if current_rank == group_dst_rank:
            tensor.copy_(tensor_tmp)

def all_gather(tensor_list,
               tensor,
               group=None,
               async_op=False):
    """
    Gathers tensors from the whole group in a list.

    Complex tensors are supported.

    Args:
        tensor_list (list[Tensor]): Output list. It should contain
            correctly-sized tensors to be used for output of the collective.
        tensor (Tensor): Tensor to be broadcast from current process.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    Examples:
        >>> # All tensors below are of torch.int64 dtype.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor_list = [torch.zero(2, dtype=torch.int64) for _ in range(2)]
        >>> tensor_list
        [tensor([0, 0]), tensor([0, 0])] # Rank 0 and 1
        >>> tensor = torch.arange(2, dtype=torch.int64) + 1 + 2 * rank
        >>> tensor
        tensor([1, 2]) # Rank 0
        tensor([3, 4]) # Rank 1
        >>> dist.all_gather(tensor_list, tensor)
        >>> tensor_list
        [tensor([1, 2]), tensor([3, 4])] # Rank 0
        [tensor([1, 2]), tensor([3, 4])] # Rank 1

        >>> # All tensors below are of torch.cfloat dtype.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor_list = [torch.zero(2, dtype=torch.cfloat) for _ in range(2)]
        >>> tensor_list
        [tensor([0.+0.j, 0.+0.j]), tensor([0.+0.j, 0.+0.j])] # Rank 0 and 1
        >>> tensor = torch.tensor([1+1j, 2+2j], dtype=torch.cfloat) + 2 * rank * (1+1j)
        >>> tensor
        tensor([1.+1.j, 2.+2.j]) # Rank 0
        tensor([3.+3.j, 4.+4.j]) # Rank 1
        >>> dist.all_gather(tensor_list, tensor)
        >>> tensor_list
        [tensor([1.+1.j, 2.+2.j]), tensor([3.+3.j, 4.+4.j])] # Rank 0
        [tensor([1.+1.j, 2.+2.j]), tensor([3.+3.j, 4.+4.j])] # Rank 1

    """
    _check_tensor_list(tensor_list, "tensor_list")
    _check_single_tensor(tensor, "tensor")
    if _rank_not_in_group(group):
        return

    tensor_list = [t if not t.is_complex() else torch.view_as_real(t) for t in tensor_list]
    tensor = tensor if not tensor.is_complex() else torch.view_as_real(tensor)

    if group is None:
        default_pg = _get_default_group()
        work = default_pg.allgather([tensor_list], [tensor])
    else:
        work = group.allgather([tensor_list], [tensor])

    if async_op:
        return work
    else:
        work.wait()

def all_gather_coalesced(output_tensor_lists,
                         input_tensor_list,
                         group=None,
                         async_op=False):
    """
    Gathers input tensors from the whole group in a list in a coalesced manner.

    Complex tensors are supported.

    Args:
        output_tensor_lists (list[list[Tensor]]): Output list. It should contain
            correctly-sized tensors to be used for output of the collective.
        input_tensor_list (list[Tensor]): Tensors to be broadcast from
            current process. At least one tensor has to be non empty.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op.

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    Example:
        we have 2 process groups, 2 ranks.
        rank 0 passes:
            input_tensor_list = [[[1, 1], [1, 1]], [2], [3, 3]]
            output_tensor_lists =
               [[[[-1, -1], [-1, -1]], [-1], [-1, -1]],
                [[[-1, -1], [-1, -1]], [-1], [-1, -1]]]
        rank 1 passes:
            input_tensor_list = [[[3, 3], [3, 3]], [5], [1, 1]]
            output_tensor_lists =
               [[[[-1, -1], [-1, -1]], [-1], [-1, -1]],
                [[[-1, -1], [-1, -1]], [-1], [-1, -1]]]
        both rank 0 and 1 get:
            output_tensor_lists =
               [[[1, 1], [1, 1]], [2], [3, 3]],
                [[3, 3], [3, 3]], [5], [1, 1]]].

    WARNING: at this time individual shape checking is not implemented across nodes.
    For example, if the rank 0 node passes [torch.rand(4), torch.rand(2)] and the
    rank 1 node passes [torch.rand(2), torch.rand(2), torch.rand(2)], the
    all_gather_coalesced operation will proceed without complaint and return
    erroneous outputs. This lack of shape checking results in significant
    performance improvements but users of this function should take extra care
    to ensure that each node passes in tensors whose shapes match across nodes.
    """
    # We only check basic compatibility with C++ params here, C++ code will
    # do shape and type checking.
    if _rank_not_in_group(group):
        return
    _check_tensor_list(input_tensor_list, "tensor_list")
    if not isinstance(output_tensor_lists, list):
        raise RuntimeError("Invalid function argument: "
                           "output_tensor_lists should be a list")
    for output_tensor_list in output_tensor_lists:
        _check_tensor_list(output_tensor_list, "output_tensor_lists")

    output_tensor_lists = [[t if not t.is_complex() else torch.view_as_real(t) for t in l] for l in output_tensor_lists]
    input_tensor_list = [t if not t.is_complex() else torch.view_as_real(t) for t in input_tensor_list]

    if group is None:
        default_pg = _get_default_group()
        work = default_pg.allgather_coalesced(
            output_tensor_lists, input_tensor_list)
    else:
        work = group.allgather_coalesced(output_tensor_lists, input_tensor_list)

    if async_op:
        return work
    else:
        work.wait()

def _validate_output_list_for_rank(my_rank, dst, gather_list):
    if dst == my_rank:
        if not gather_list:
            raise ValueError(
                "Argument ``gather_list`` must be specified on destination rank."
            )
    elif gather_list:
        raise ValueError(
            "Argument ``gather_list`` must NOT be specified "
            "on non-destination ranks."
        )


def gather(tensor,
           gather_list=None,
           dst=0,
           group=None,
           async_op=False):
    """
    Gathers a list of tensors in a single process.

    Args:
        tensor (Tensor): Input tensor.
        gather_list (list[Tensor], optional): List of appropriately-sized
            tensors to use for gathered data (default is None, must be specified
            on the destination rank)
        dst (int, optional): Destination rank (default is 0)
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    """
    _check_single_tensor(tensor, "tensor")

    # Parameter ``gather_list`` may be left unspecified on non-dst ranks.
    if gather_list:
        _check_tensor_list(gather_list, "gather_list")
    else:
        gather_list = []

    if _rank_not_in_group(group):
        return

    my_rank = get_rank()
    _validate_output_list_for_rank(my_rank, dst, gather_list)
    output_tensors = [gather_list] if dst == my_rank else []
    input_tensors = [tensor]

    opts = GatherOptions()
    opts.rootRank = dst

    if group is None or group is GroupMember.WORLD:
        default_pg = _get_default_group()
        work = default_pg.gather(output_tensors, input_tensors, opts)
    else:
        group_dst_rank = _get_group_rank(group, dst)
        opts.rootRank = group_dst_rank
        work = group.gather(output_tensors, input_tensors, opts)

    if async_op:
        return work
    else:
        work.wait()


def scatter(tensor,
            scatter_list=None,
            src=0,
            group=None,
            async_op=False):
    """
    Scatters a list of tensors to all processes in a group.

    Each process will receive exactly one tensor and store its data in the
    ``tensor`` argument.

    Args:
        tensor (Tensor): Output tensor.
        scatter_list (list[Tensor]): List of tensors to scatter (default is
            None, must be specified on the source rank)
        src (int): Source rank (default is 0)
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group

    """
    _check_single_tensor(tensor, "tensor")

    # Parameter ``scatter_list`` may be left unspecified on non-src ranks.
    if scatter_list:
        _check_tensor_list(scatter_list, "scatter_list")
    else:
        scatter_list = []

    if _rank_not_in_group(group):
        return

    my_rank = get_rank()
    if src == my_rank:
        if not scatter_list:
            raise ValueError("Argument ``scatter_list`` must be specified "
                             "on source rank.")
        input_tensors = [scatter_list]
        output_tensors = [tensor]
    else:
        if scatter_list:
            raise ValueError("Argument ``scatter_list`` must NOT be specified "
                             "on non-source ranks.")
        input_tensors = []
        output_tensors = [tensor]

    opts = ScatterOptions()
    opts.rootRank = src

    if group is None or group is GroupMember.WORLD:
        default_pg = _get_default_group()
        work = default_pg.scatter(output_tensors, input_tensors, opts)
    else:
        group_src_rank = _get_group_rank(group, src)
        opts.rootRank = group_src_rank
        work = group.scatter(output_tensors, input_tensors, opts)

    if async_op:
        return work
    else:
        work.wait()


def reduce_scatter(output,
                   input_list,
                   op=ReduceOp.SUM,
                   group=None,
                   async_op=False):
    """
    Reduces, then scatters a list of tensors to all processes in a group.

    Args:
        output (Tensor): Output tensor.
        input_list (list[Tensor]): List of tensors to reduce and scatter.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op.

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group.

    """
    _check_single_tensor(output, "output")
    _check_tensor_list(input_list, "input_list")
    if _rank_not_in_group(group):
        return

    opts = ReduceScatterOptions()
    opts.reduceOp = op

    if group is None:
        default_pg = _get_default_group()
        work = default_pg.reduce_scatter([output], [input_list], opts)
    else:
        work = group.reduce_scatter([output], [input_list], opts)

    if async_op:
        return work
    else:
        work.wait()


def all_to_all_single(output_tensor,
                      input_tensor,
                      output_split_sizes=None,
                      input_split_sizes=None,
                      group=None,
                      async_op=False):
    """
    Each process splits input tensor and then scatters the split list
    to all processes in a group. Then concatenate the received tensors from all
    the processes in the group and return single output tensor.

    Args:
        output (Tensor): Gathered cancatenated output tensor.
        input (Tensor): Input tensor to scatter.
        output_split_sizes: (list[Int], optional): Output split sizes for dim 0
            if specified None or empty, dim 0 of ``output`` tensor must divide
            equally by ``world_size``.
        input_split_sizes: (list[Int], optional): Input split sizes for dim 0
            if specified None or empty, dim 0 of ``input`` tensor must divide
            equally by ``world_size``.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op.

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group.

    .. warning::
        `all_to_all_single` is experimental and subject to change.

    Examples:
        >>> input = torch.arange(4) + rank * 4
        >>> input
        tensor([0, 1, 2, 3])     # Rank 0
        tensor([4, 5, 6, 7])     # Rank 1
        tensor([8, 9, 10, 11])   # Rank 2
        tensor([12, 13, 14, 15]) # Rank 3
        >>> output = torch.empty([4], dtype=torch.int64)
        >>> dist.all_to_all_single(output, input)
        >>> output
        tensor([0, 4, 8, 12])    # Rank 0
        tensor([1, 5, 9, 13])    # Rank 1
        tensor([2, 6, 10, 14])   # Rank 2
        tensor([3, 7, 11, 15])   # Rank 3

        >>> # Essentially, it is similar to following operation:
        >>> scatter_list = list(input.chunk(world_size))
        >>> gather_list  = list(output.chunk(world_size))
        >>> for i in range(world_size):
        >>>   dist.scatter(gather_list[i], scatter_list if i == rank else [], src = i)

        >>> # Another example with uneven split
        >>> input
        tensor([0, 1, 2, 3, 4, 5])                                       # Rank 0
        tensor([10, 11, 12, 13, 14, 15, 16, 17, 18])                     # Rank 1
        tensor([20, 21, 22, 23, 24])                                     # Rank 2
        tensor([30, 31, 32, 33, 34, 35, 36])                             # Rank 3
        >>> input_splits
        [2, 2, 1, 1]                                                     # Rank 0
        [3, 2, 2, 2]                                                     # Rank 1
        [2, 1, 1, 1]                                                     # Rank 2
        [2, 2, 2, 1]                                                     # Rank 3
        >>> output_splits
        [2, 3, 2, 2]                                                     # Rank 0
        [2, 2, 1, 2]                                                     # Rank 1
        [1, 2, 1, 2]                                                     # Rank 2
        [1, 2, 1, 1]                                                     # Rank 3
        >>> output = ...
        >>> dist.all_to_all_single(output, input, output_splits, input_splits)
        >>> output
        tensor([ 0,  1, 10, 11, 12, 20, 21, 30, 31])                     # Rank 0
        tensor([ 2,  3, 13, 14, 22, 32, 33])                             # Rank 1
        tensor([ 4, 15, 16, 23, 34, 35])                                 # Rank 2
        tensor([ 5, 17, 18, 24, 36])                                     # Rank 3
    """
    if _rank_not_in_group(group):
        return

    opts = AllToAllOptions()
    _check_single_tensor(output_tensor, "output")
    _check_single_tensor(input_tensor, "input")
    output_split_sizes = [] if output_split_sizes is None else output_split_sizes
    input_split_sizes = [] if input_split_sizes is None else input_split_sizes

    if group is None:
        default_pg = _get_default_group()
        work = default_pg.alltoall_base(output_tensor, input_tensor, output_split_sizes, input_split_sizes, opts)
    else:
        work = group.alltoall_base(output_tensor, input_tensor, output_split_sizes, input_split_sizes, opts)

    if async_op:
        return work
    else:
        work.wait()

def all_to_all(output_tensor_list,
               input_tensor_list,
               group=None,
               async_op=False):
    """
    Each process scatters list of input tensors to all processes in a group and
    return gathered list of tensors in output list.

    Args:
        output_tensor_list (list[Tensor]): List of tensors to be gathered one
            per rank.
        input_tensor_list (list[Tensor]): List of tensors to scatter one per rank.
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op.

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group.

    .. warning::
        `all_to_all` is experimental and subject to change.

    Examples:
        >>> input = torch.arange(4) + rank * 4
        >>> input = list(input.chunk(4))
        >>> input
        [tensor([0]), tensor([1]), tensor([2]), tensor([3])]     # Rank 0
        [tensor([4]), tensor([5]), tensor([6]), tensor([7])]     # Rank 1
        [tensor([8]), tensor([9]), tensor([10]), tensor([11])]   # Rank 2
        [tensor([12]), tensor([13]), tensor([14]), tensor([15])] # Rank 3
        >>> output = list(torch.empty([4], dtype=torch.int64).chunk(4))
        >>> dist.all_to_all(output, input)
        >>> output
        [tensor([0]), tensor([4]), tensor([8]), tensor([12])]    # Rank 0
        [tensor([1]), tensor([5]), tensor([9]), tensor([13])]    # Rank 1
        [tensor([2]), tensor([6]), tensor([10]), tensor([14])]   # Rank 2
        [tensor([3]), tensor([7]), tensor([11]), tensor([15])]   # Rank 3

        >>> # Essentially, it is similar to following operation:
        >>> scatter_list = input
        >>> gather_list  = output
        >>> for i in range(world_size):
        >>>   dist.scatter(gather_list[i], scatter_list if i == rank else [], src = i)

        >>> input
        tensor([0, 1, 2, 3, 4, 5])                                       # Rank 0
        tensor([10, 11, 12, 13, 14, 15, 16, 17, 18])                     # Rank 1
        tensor([20, 21, 22, 23, 24])                                     # Rank 2
        tensor([30, 31, 32, 33, 34, 35, 36])                             # Rank 3
        >>> input_splits
        [2, 2, 1, 1]                                                     # Rank 0
        [3, 2, 2, 2]                                                     # Rank 1
        [2, 1, 1, 1]                                                     # Rank 2
        [2, 2, 2, 1]                                                     # Rank 3
        >>> output_splits
        [2, 3, 2, 2]                                                     # Rank 0
        [2, 2, 1, 2]                                                     # Rank 1
        [1, 2, 1, 2]                                                     # Rank 2
        [1, 2, 1, 1]                                                     # Rank 3
        >>> input = list(input.split(input_splits))
        >>> input
        [tensor([0, 1]), tensor([2, 3]), tensor([4]), tensor([5])]                   # Rank 0
        [tensor([10, 11, 12]), tensor([13, 14]), tensor([15, 16]), tensor([17, 18])] # Rank 1
        [tensor([20, 21]), tensor([22]), tensor([23]), tensor([24])]                 # Rank 2
        [tensor([30, 31]), tensor([32, 33]), tensor([34, 35]), tensor([36])]         # Rank 3
        >>> output = ...
        >>> dist.all_to_all(output, input)
        >>> output
        [tensor([0, 1]), tensor([10, 11, 12]), tensor([20, 21]), tensor([30, 31])]   # Rank 0
        [tensor([2, 3]), tensor([13, 14]), tensor([22]), tensor([32, 33])]           # Rank 1
        [tensor([4]), tensor([15, 16]), tensor([23]), tensor([34, 35])]              # Rank 2
        [tensor([5]), tensor([17, 18]), tensor([24]), tensor([36])]                  # Rank 3
    """
    if _rank_not_in_group(group):
        return

    opts = AllToAllOptions()
    _check_tensor_list(output_tensor_list, "output_tensor_list")
    _check_tensor_list(input_tensor_list, "input_tensor_list")

    if group is None:
        default_pg = _get_default_group()
        work = default_pg.alltoall(output_tensor_list, input_tensor_list, opts)
    else:
        work = group.alltoall(output_tensor_list, input_tensor_list, opts)

    if async_op:
        return work
    else:
        work.wait()



def barrier(group=GroupMember.WORLD,
            async_op=False,
            device_ids=None):

    """
    Synchronizes all processes.

    This collective blocks processes until the whole group enters this function,
    if async_op is False, or if async work handle is called on wait().

    Args:
        group (ProcessGroup, optional): The process group to work on. If None,
            the default process group will be used.
        async_op (bool, optional): Whether this op should be an async op
        device_ids ([int], optional): List of device/GPU ids.
                                      Valid only for NCCL backend.

    Returns:
        Async work handle, if async_op is set to True.
        None, if not async_op or if not part of the group
    """
    if _rank_not_in_group(group):
        return

    opts = BarrierOptions()
    if device_ids is not None:
        if get_backend(group) != Backend.NCCL:
            raise RuntimeError("Function argument device_ids not supported "
                               "for the selected backend {}".format(get_backend(group)))
        if isinstance(device_ids, list):
            opts.device_ids = device_ids
        else:
            raise RuntimeError("Invalid function argument: "
                               "device_ids type should be List[int]")

    if group is None:
        default_pg = _get_default_group()
        work = default_pg.barrier(opts=opts)
    else:
        work = group.barrier(opts=opts)

    if async_op:
        return work
    else:
        work.wait()


def new_group(ranks=None, timeout=default_pg_timeout, backend=None):
    """
    Creates a new distributed group.

    This function requires that all processes in the main group (i.e. all
    processes that are part of the distributed job) enter this function, even
    if they are not going to be members of the group. Additionally, groups
    should be created in the same order in all processes.

    .. warning::
        Using multiple process groups with the ``NCCL`` backend concurrently
        is not safe and the user should perform explicit synchronization in
        their application to ensure only one process group is used at a time.
        This means collectives from one process group should have completed
        execution on the device (not just enqueued since CUDA execution is
        async) before collectives from another process group are enqueued.
        See `Using multiple NCCL communicators concurrently <https://docs.nvid
        ia.com/deeplearning/nccl/user-guide/docs/usage/communicators.html#using
        -multiple-nccl-communicators-concurrently>`_ for more details.

    Args:
        ranks (list[int]): List of ranks of group members. If ``None``, will be
            set to all ranks. Default is ``None``.
        timeout (timedelta, optional): Timeout for operations executed against
            the process group. Default value equals 30 minutes.
            This is only applicable for the ``gloo`` backend.
        backend (str or Backend, optional): The backend to use. Depending on
            build-time configurations, valid values are ``gloo`` and ``nccl``.
            By default uses the same backend as the global group. This field
            should be given as a lowercase string (e.g., ``"gloo"``), which can
            also be accessed via :class:`Backend` attributes (e.g.,
            ``Backend.GLOO``).

    Returns:
        A handle of distributed group that can be given to collective calls.
    """


    global _pg_group_ranks

    default_pg = _get_default_group()
    default_backend, default_store = _pg_map[default_pg]
    global_rank = default_pg.rank()
    global_world_size = default_pg.size()

    # Default to the same backend as the global process group
    # if the backend is not specified.
    if not backend:
        backend = default_backend

    # checks the input ranks
    if ranks is not None:
        ranks = sorted(ranks)
        group_world_size = len(ranks)
        if group_world_size > global_world_size:
            raise RuntimeError("the new group's world size should be less or "
                               "equal to the world size set by "
                               "init_process_group")
        # check ranks' sanity
        for rank in ranks:
            if rank < 0 or rank >= global_world_size:
                raise RuntimeError("The new group's rank should be within the "
                                   "the world_size set by init_process_group")
        if global_rank in ranks:
            group_rank = ranks.index(global_rank)
        else:
            group_rank = None
    else:
        ranks = list(range(global_world_size))
        group_world_size = global_world_size
        group_rank = global_rank

    backend = Backend(backend)
    pg = _new_process_group_helper(group_world_size,
                                   group_rank,
                                   ranks,
                                   backend,
                                   default_store,
                                   timeout=timeout)

    # Create the global rank to group rank mapping
    _pg_group_ranks[pg] = {
        global_rank: group_rank
        for group_rank, global_rank in enumerate(ranks)
    }

    # barrier at the end to ensure that once we return from this method, all
    # process groups including global variables are updated correctly on all
    # ranks.
    if backend == Backend.MPI:
        # MPI doesn't have store.
        barrier()
    else:
        # Use store based barrier here since barrier() used a bunch of
        # default devices and messes up NCCL internal state.
        _store_based_barrier(global_rank, default_store, timeout)

    return pg
