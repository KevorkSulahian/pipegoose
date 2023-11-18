from copy import deepcopy
from typing import Tuple

import torch
import torch.distributed as dist
from einops import rearrange
from torch import nn
from torchtyping import TensorType

from pipegoose.distributed.parallel_context import ParallelContext
from pipegoose.distributed.parallel_mode import ParallelMode
from pipegoose.nn.tensor_parallel._functional import all_reduce


class Experts(nn.Module):
    """A collection of experts in an expert layer."""

    def __init__(
        self,
        num_experts: int,
        expert: nn.Module,
        enable_tensor_parallel: bool,
        parallel_context: ParallelContext,
    ):
        super().__init__()
        self.num_experts = num_experts
        self.enable_tensor_parallel = enable_tensor_parallel
        self.parallel_context = parallel_context

        if enable_tensor_parallel is True:
            self.num_local_experts = num_experts
        else:
            expert_parallel_size = parallel_context.get_world_size(ParallelMode.TENSOR)
            self.num_local_experts = num_experts // expert_parallel_size

        expert = expert() if not isinstance(expert, nn.Module) else expert
        self.experts = nn.ModuleList([deepcopy(expert) for _ in range(self.num_local_experts)])

    def forward(
        self,
        inputs: TensorType["batch_size", "seq_len", "d_model"],
        dispatch_order: TensorType["batch_size * seq_len"],
        *args,
        **kwargs,
    ) -> TensorType["batch_size", "seq_len", "d_model"]:
        outputs = torch.zeros_like(inputs)
        for expert_idx, expert in enumerate(self.experts):
            print(f"expert_idx={expert_idx}")
            dispatched_inputs, indices = self._get_dispatch_inputs(inputs, dispatch_order, expert_idx)
            if dispatched_inputs.numel() == 0:
                # NOTE: if there are no tokens to dispatch to the expert, skip the expert
                continue

            outputs.view(-1, outputs.size(-1))[indices] = expert(dispatched_inputs, *args[1][:, indices], **kwargs)

        all_reduce(
            outputs,
            op=dist.ReduceOp.SUM,
            parallel_context=self.parallel_context,
            parallel_mode=ParallelMode.TENSOR,
        )

        return outputs

    def _get_dispatch_inputs(
        self,
        inputs: TensorType["batch_size", "seq_len", "d_model"],
        dispatch_order: TensorType["batch_size * seq_len"],
        expert_idx: int,
    ) -> Tuple[TensorType["batch_size * seq_len", "d_model"], TensorType["batch_size * seq_len"]]:
        """Dispatch embeddings to the corresponding expert."""

        def get_global_expert_idx(expert_idx: int) -> int:
            rank = self.parallel_context.get_local_rank(ParallelMode.TENSOR)
            global_expert_idx = rank * self.num_local_experts + expert_idx
            return global_expert_idx

        global_expert_idx = get_global_expert_idx(expert_idx)
        token_indices = (dispatch_order == global_expert_idx).nonzero(as_tuple=True)[0]
        inputs = rearrange(inputs, "b s d -> (b s) d")
        dispatched_inputs = inputs[token_indices]
        return dispatched_inputs, token_indices
