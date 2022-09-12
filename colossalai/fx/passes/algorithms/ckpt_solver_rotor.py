from typing import List, Set, Tuple, Dict
import torch
from torch.fx import GraphModule, Node
from colossalai.fx.graph_module import ColoGraphModule
import math
from .linearize import linearize
from .utils import *
from colossalai.fx.profiler import profile_function, profile_module
from colossalai.fx.passes.meta_info_prop import MetaInfoProp
from colossalai.fx.codegen.activation_checkpoint_codegen import _find_nested_ckpt_regions


# this is the python compute table code from rotor
# https://gitlab.inria.fr/hiepacs/rotor
# paper link: https://hal.inria.fr/hal-02352969
def _compute_table(chain: Chain, mmax) -> Tuple:
    """Returns the optimal table: a tuple containing: 
    Opt[m][lmin][lmax] with lmin = 0...chain.length
         and lmax = lmin...chain.length (lmax is not included) and m = 0...mmax
    what[m][lmin][lmax] is (True,) if the optimal choice is a chain checkpoint
                           (False, j) if the optimal choice is a leaf checkpoint of length j
    The computation uses dynamic programming"""

    fw = chain.fweight + [0]    ## forward time
    bw = chain.bweight    ## backward time, not used
    cw = chain.cweight + [0]    ## size of x (and of y)
    cbw = chain.cbweight + [0]    ## size of xbar
    fwd_tmp = chain.fwd_tmp + [0]
    bwd_tmp = chain.bwd_tmp + [0]

    # Build table
    opt = [[{} for _ in range(chain.length + 1)] for _ in range(mmax + 1)]
    what = [[{} for _ in range(chain.length + 1)] for _ in range(mmax + 1)]
    ## Last one is a dict because its indices go from i to l. Renumbering will wait for C implementation

    # Initialize borders of the tables for lmax-lmin = 0
    for m in range(mmax + 1):
        for i in range(chain.length + 1):
            #lmax-lmin = 0
            limit = max(cw[i + 1] + cbw[i + 1] + fwd_tmp[i], cw[i + 1] + cbw[i + 1] + bwd_tmp[i])
            if m >= limit:    ## Equation (1)
                opt[m][i][i] = fw[i] + bw[i]
            else:
                opt[m][i][i] = float("inf")

    # Compute everything
    for m in range(mmax + 1):
        for d in range(1, chain.length + 1):
            for i in range(chain.length + 1 - d):
                # for idx in range(i+1, chain.length + 1):
                idx = i + d
                mmin = cw[idx + 1] + cw[i + 1] + fwd_tmp[i]
                if idx > i + 1:
                    mmin = max(mmin, cw[idx + 1] + max(cw[j] + cw[j + 1] + fwd_tmp[j] for j in range(i + 1, idx)))
                if m < mmin:
                    opt[m][i][idx] = float("inf")
                else:
                    leaf_checkpoints = [(j, sum(fw[i:j]) + opt[m - cw[j]][j][idx] + opt[m][i][j - 1])
                                        for j in range(i + 1, idx + 1)
                                        if m >= cw[j]]
                    if leaf_checkpoints:
                        best_leaf = min(leaf_checkpoints, key=lambda t: t[1])
                    else:
                        best_leaf = None
                    if m >= cbw[i + 1]:
                        chain_checkpoint = opt[m][i][i] + opt[m - cbw[i + 1]][i + 1][idx]
                    else:
                        chain_checkpoint = float("inf")
                    if best_leaf and best_leaf[1] <= chain_checkpoint:
                        opt[m][i][idx] = best_leaf[1]
                        what[m][i][idx] = (False, best_leaf[0])
                    else:
                        opt[m][i][idx] = chain_checkpoint
                        what[m][i][idx] = (True,)
    return (opt, what)


def _rec(chain: Chain, lmin, lmax, cmem, opt_table):
    """ chain : the class describing the AC graph
        lmin : index of the first forward to execute
        lmax : upper bound index of the last forward to execute (not included)
        cmem : number of available memory slots
        Return the optimal sequence of makespan Opt_hete[cmem][lmin][lmax-lmin]"""
    if cmem <= 0:
        raise ValueError("Can not process a chain with negative memory {cmem}".format(cmem=cmem))
    opt, what = opt_table
    sequence = Sequence(Function("Persistent", lmax - lmin, cmem))
    if opt[cmem][lmin][lmax] == float("inf"):
        raise ValueError("Can not process this chain from index {lmin} to {lmax} with memory {cmem}".format(lmin=lmin,
                                                                                                            lmax=lmax,
                                                                                                            cmem=cmem))
    if lmin == lmax:
        if lmin == chain.length:
            sequence.insert(Loss())
        else:
            sequence.insert(ForwardEnable(lmin))
            sequence.insert(Backward(lmin))
        return sequence

    if what[cmem][lmin][lmax][0]:
        sequence.insert(ForwardEnable(lmin))
        sequence.insert_sequence(_rec(chain, lmin + 1, lmax, cmem - chain.cbweight[lmin + 1], opt_table))
        sequence.insert(Backward(lmin))
    else:
        j = what[cmem][lmin][lmax][1]
        sequence.insert(ForwardCheck(lmin))
        for k in range(lmin + 1, j):
            sequence.insert(ForwardNograd(k))
        sequence.insert_sequence(_rec(chain, j, lmax, cmem - chain.cweight[j], opt_table))
        sequence.insert_sequence(_rec(chain, lmin, j - 1, cmem, opt_table))
    return sequence


def _discretize(mem_unit, values):
    return [math.ceil(value / mem_unit) for value in values]


def _compute_size(obj: torch.Tensor) -> int:
    return obj.numel() * obj.element_size()


def _compute_output_size(node: List[Node]) -> int:
    """Compute the output size of a node

    Args:
        node (List[Node]): node, list of torch.fx.Node

    Returns:
        int: output size
    """

    return node[-1].meta['tensor_meta'].numel * \
    torch.tensor([], dtype=node[-1].meta['tensor_meta'].dtype).element_size()


def _get_inplace(node: Node) -> bool:
    """Get the inplace argument from torch.fx.Node

    Args:
        node (Node): torch.fx.Node

    Returns:
        bool: indicates whether this op is inplace
    """

    is_inplace = False
    if node.op == "call_function":
        is_inplace = node.kwargs.get("inplace", False)
    elif node.op == "call_module":
        is_inplace = getattr(node.graph.owning_module.get_submodule(node.target), "inplace", False)

    return is_inplace


def _fwd_xbar(node: List[Node]) -> int:
    """Get the forward xbar of a node

    Args:
        node (List[Node]): List of torch.fx Node, 
        indicates a node in linearized graph

    Returns:
        int: xbar size, unit Byte
    """

    xbar = 0
    for n in node:
        xbar += n.fwd_tmp + n.fwd_out
    return xbar


def _fwd_time(node: List[Node]) -> int:
    """Get the foward time of a node

    Args:
        node (List[Node]): List of torch.fx Node,
        indicates a node in linearized graph

    Returns:
        int: foward time, extimated by flops count
    """

    fwd_time = 0
    for n in node:
        # minimum flop count is needed
        fwd_time += max(n.fwd_flop, 1)
    return fwd_time


def _bwd_time(node: List[Node]) -> int:
    """Get the backward time of a node

    Args:
        node (List[Node]): List of torch.fx Node,
        indicates a node in linearized graph

    Returns:
        int: backward time, extimated by flops count
    """

    bwd_time = 0
    for n in node:
        # minimum flop count is needed
        bwd_time += max(n.bwd_flop, 1)
    return bwd_time


def _get_bwd_tmp(node: List[Node]) -> int:
    """Get the backward temp memory of a node

    Args:
        node (List[Node]): List of torch.fx Node,
        indicates a node in linearized graph

    Returns:
        int: backward temp memory, unit Byte
    """

    def _get_deps_size():
        deps_size = 0
        for key in deps.keys():
            deps_size += key.bwd_out

        return deps_size

    bwd_tmp = 0
    deps = {}

    # add all the users for last node into deps,
    # as those nodes' gradient out will be stored in memory
    for son in node[-1].users:
        deps[son] = 1
    for n in reversed(node):
        bwd_tmp = max(bwd_tmp, _get_deps_size() + n.bwd_tmp)
        deps[n] = len(n._input_nodes)
        for son in n.users:
            deps[son] -= 1

        for key in list(deps.keys()):
            if deps[key] == 0:
                del deps[key]

    return bwd_tmp


def _construct_chain(node_list: List[List[Node]], data, mem_unit: int) -> Chain:

    fwd_time = []
    bwd_time = []

    if isinstance(data, torch.Tensor):
        xbar_sizes = [_compute_size(data)]
        x_sizes = [_compute_size(data)]
    elif isinstance(data, list) or isinstance(data, tuple):
        xbar_sizes = [sum([_compute_size(obj) for obj in data])]
        x_sizes = [sum([_compute_size(obj) for obj in data])]
    elif isinstance(data, dict):
        xbar_sizes = [sum([_compute_size(obj) for obj in data.values()])]
        x_sizes = [sum([_compute_size(obj) for obj in data.values()])]

    # currently we can't get the temp memory needed in fwd
    tmp_fwd = [0] * len(node_list)
    tmp_bwd = []

    for idx, node in enumerate(node_list):
        fwd_time.append(_fwd_time(node))
        bwd_time.append(_bwd_time(node))
        x_sizes.append(_compute_output_size(node))
        xbar_sizes.append(max(x_sizes[-1], _fwd_xbar(node)))
        tmp_bwd.append(_get_bwd_tmp(node))

        # if a node with only one inplace op, we need to let x_bar = 0
        if len(node) == 1 and _get_inplace(node[0]):
            xbar_sizes[-1] = 0

    bwd_time.append(0)

    # currently we view loss backward temp as zero
    tmp_bwd.append(0)

    xbar_sizes = _discretize(mem_unit, xbar_sizes)
    x_sizes = _discretize(mem_unit, x_sizes)
    tmp_fwd = _discretize(mem_unit, tmp_fwd)
    tmp_bwd = _discretize(mem_unit, tmp_bwd)

    return Chain(fwd_time, bwd_time, x_sizes, xbar_sizes, tmp_fwd, tmp_bwd)


def _annotate_from_sequence(sequence: Sequence, node_list: List[List[Node]]):
    op_list = sequence.list_operations()
    loss_op = next(op for op in op_list if isinstance(op, Loss))
    fwd_list = op_list[:op_list.index(loss_op)]
    bwd_list = op_list[op_list.index(loss_op) + 1:]
    ckpt_idx = 0
    in_ckpt = False
    ckpt_region = []

    # forward annotation
    for idx, op in enumerate(fwd_list, 0):
        if in_ckpt:
            if isinstance(op, ForwardNograd):
                ckpt_region.append(idx)

            elif isinstance(op, ForwardEnable):
                in_ckpt = False
                for node_idx in ckpt_region:
                    for n in node_list[node_idx]:
                        setattr(n, "activation_checkpoint", [ckpt_idx])

                ckpt_idx += 1
                ckpt_region = []

            elif isinstance(op, ForwardCheck):
                for node_idx in ckpt_region:
                    for n in node_list[node_idx]:
                        setattr(n, "activation_checkpoint", [ckpt_idx])

                ckpt_idx += 1
                ckpt_region = [idx]

        else:
            if isinstance(op, ForwardCheck):
                in_ckpt = True
                ckpt_region.append(idx)

    # annotate the backward if there is any nested activation checkpoint
    in_recompute = False
    for op in bwd_list:
        if in_recompute:
            if isinstance(op, ForwardNograd):
                ckpt_region.append(op.index)

            elif isinstance(op, ForwardEnable):
                for node_idx in ckpt_region:
                    for n in node_list[node_idx]:
                        n.activation_checkpoint.append(ckpt_idx)

                ckpt_idx += 1
                ckpt_region = []

            elif isinstance(op, ForwardCheck):
                for node_idx in ckpt_region:
                    for n in node_list[node_idx]:
                        n.activation_checkpoint.append(ckpt_idx)

                ckpt_idx += 1
                ckpt_region = [op.index]

            elif isinstance(op, Backward):
                for node_idx in ckpt_region:
                    for n in node_list[node_idx]:
                        n.activation_checkpoint.append(ckpt_idx)

                in_recompute = False

        else:
            if not isinstance(op, Backward):
                in_recompute = True
                ckpt_idx = 0
                ckpt_region = []
                if isinstance(op, ForwardCheck):
                    ckpt_region.append(op.index)

    # postprocess, make sure every activation checkpoint label in the
    # same activation checkpoint region (level = 0) has the same length
    op_list = []
    for node in node_list:
        op_list += node
    ckpt_regions = _find_nested_ckpt_regions(op_list)
    for (start_idx, end_idx) in ckpt_regions:
        nested_length = max(len(op_list[idx].activation_checkpoint) for idx in range(start_idx, end_idx + 1))
        for idx in range(start_idx, end_idx + 1):
            op_list[idx].activation_checkpoint += [None] * (nested_length - len(op_list[idx].activation_checkpoint))


def solver_rotor(gm: ColoGraphModule,
                 data,
                 mem_limit: int,
                 mem_slots: int = 500,
                 cnode: List[str] = None,
                 eps: float = 0.02) -> ColoGraphModule:
    """solver that automatically find activation checkpoint in rotor's manner

    Args:
        gm (ColoGraphModule): ColoGraphModule generated by tracing model.
        data (torch.Tensor): input data.
        mem_limit (int): memory budget in Byte.
        mem_slots (int, optional): number of slots for discretizing memory budget. Defaults to 500.
        cnode (List[Node], optional): common node list for linearize. Defaults to None.
        eps (float): epsilon for memory decay. Defaults to 0.02

    Returns:
        ColoGraphModule: annotated ColoGraphModuled with __sequence__ attribute
    """

    node_list = linearize(gm, cnode)
    mem_unit = mem_limit * (1.0 - eps) // mem_slots
    MetaInfoProp(gm).run(data)
    chain: Chain = _construct_chain(node_list, data, mem_unit)
    opt_table = _compute_table(chain, mem_slots)
    sequence = _rec(chain, 0, chain.length, mem_slots - chain.cweight[0], opt_table)
    _annotate_from_sequence(sequence, node_list)

    # set __sequence__ attribute to GraphModule
    setattr(gm, "__sequence__", sequence)
    return gm
