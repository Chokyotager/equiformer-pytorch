from math import sqrt
from itertools import product
from collections import namedtuple

from typing import Optional, Union, Tuple
from beartype import beartype

import torch
import torch.nn.functional as F
from torch import nn, einsum

from equiformer_pytorch.basis import get_basis
from equiformer_pytorch.utils import exists, default, uniq, batched_index_select, masked_mean, to_order, cast_tuple, safe_cat, fast_split, rand_uniform, broadcat

from einops import rearrange, repeat

# constants

Return = namedtuple('Return', ['type0', 'type1'])

# fiber functions

@beartype
def fiber_product(
    fiber_in: Tuple[int, ...],
    fiber_out: Tuple[int, ...]
):
    fiber_in, fiber_out = tuple(map(lambda t: [(degree, dim) for degree, dim in enumerate(t)], (fiber_in, fiber_out)))
    return product(fiber_in, fiber_out)

@beartype
def fiber_and(
    fiber_in: Tuple[int, ...],
    fiber_out: Tuple[int, ...]
):
    fiber_in = [(degree, dim) for degree, dim in enumerate(fiber_in)]
    fiber_out_degrees = set(range(len(fiber_out)))

    out = []
    for degree, dim in fiber_in:
        if degree not in fiber_out_degrees:
            continue

        dim_out = fiber_out[degree]
        out.append((degree, dim, dim_out))

    return out

# helper functions

def get_tensor_device_and_dtype(features):
    _, first_tensor = next(iter(features.items()))
    return first_tensor.device, first_tensor.dtype

def residual_fn(x, residual):
    out = {}
    for degree, tensor in x.items():
        out[degree] = tensor

        if degree not in residual:
            continue

        out[degree] = out[degree] + residual[degree]
    return out

# classes

@beartype
class Linear(nn.Module):
    def __init__(
        self,
        fiber_in: Tuple[int, ...],
        fiber_out: Tuple[int, ...]
    ):
        super().__init__()
        self.weights = nn.ParameterList([])
        self.degrees = []

        for (degree, dim_in, dim_out) in fiber_and(fiber_in, fiber_out):
            self.weights.append(nn.Parameter(torch.randn(dim_in, dim_out) / sqrt(dim_in)))
            self.degrees.append(degree)

    def forward(self, x):
        out = {}

        for degree, weight in zip(self.degrees, self.weights):
            out[degree] = einsum('b n d m, d e -> b n e m', x[degree], weight)

        return out

@beartype
class Norm(nn.Module):
    def __init__(
        self,
        fiber: Tuple[int, ...],
        eps = 1e-12,
    ):
        """
        deviates from the paper slightly, will use rmsnorm throughout (no mean centering or bias, even for type0 fatures)
        this has been proven at scale for a number of models, including T5 and alphacode
        """

        super().__init__()
        self.eps = eps
        self.transforms = nn.ParameterList([])

        for degree, dim in enumerate(fiber):
            self.transforms.append(nn.Parameter(torch.ones(dim, 1)))

    def forward(self, features):
        output = {}

        for scale, (degree, t) in zip(self.transforms, features.items()):
            dim = t.shape[-2]

            l2normed = t.norm(dim = -1, keepdim = True)
            rms = l2normed.norm(dim = -2, keepdim = True) * (dim ** -0.5)

            output[degree] = t / rms.clamp(min = self.eps) * scale

        return output

@beartype
class Gate(nn.Module):
    def __init__(
        self,
        fiber: Tuple[int, ...]
    ):
        super().__init__()

        type0_dim = fiber[0]
        dim_gate = sum(fiber[1:])

        assert type0_dim > dim_gate, 'sum of channels from rest of the degrees must be less than the channels in type 0, as they would be used up for gating and subtracted out'

        self.fiber = fiber
        self.num_degrees = len(fiber)
        self.type0_dim_split = [*fiber[1:], type0_dim - dim_gate]

    def forward(self, x):
        output = {}

        type0_tensor = x[0]
        *gates, type0_tensor = type0_tensor.split(self.type0_dim_split, dim = -2)

        # silu for type 0

        output = {0: F.silu(type0_tensor)}

        # sigmoid gate the higher types

        for degree, gate in zip(range(1, self.num_degrees), gates):
            output[degree] = x[degree] * gate.sigmoid()

        return output

@beartype
class Conv(nn.Module):
    def __init__(
        self,
        fiber_in: Tuple[int, ...],
        fiber_out: Tuple[int, ...],
        self_interaction = True,
        pool = True,
        edge_dim = 0,
        splits = 4
    ):
        super().__init__()
        self.fiber_in = fiber_in
        self.fiber_out = fiber_out
        self.edge_dim = edge_dim
        self.self_interaction = self_interaction

        self.kernel_unary = nn.ModuleDict()

        self.splits = splits # for splitting the computation of kernel and basis, to reduce peak memory usage

        for (di, mi), (do, mo) in fiber_product(self.fiber_in, self.fiber_out):
            self.kernel_unary[f'({di},{do})'] = PairwiseConv(di, mi, do, mo, edge_dim = edge_dim)

        self.pool = pool

        if self_interaction:
            assert self.pool, 'must pool edges if followed with self interaction'
            self.self_interact = Linear(fiber_in, fiber_out)

    def forward(
        self,
        inp,
        edge_info,
        rel_dist = None,
        basis = None
    ):
        splits = self.splits
        neighbor_indices, neighbor_masks, edges = edge_info
        rel_dist = rearrange(rel_dist, 'b m n -> b m n 1')

        kernels = {}
        outputs = {}

        # split basis

        basis_keys = basis.keys()
        split_basis_values = list(zip(*list(map(lambda t: fast_split(t, splits, dim = 1), basis.values()))))
        split_basis = list(map(lambda v: dict(zip(basis_keys, v)), split_basis_values))

        # go through every permutation of input degree type to output degree type

        for degree_out, _ in enumerate(self.fiber_out):
            output = 0

            for degree_in, m_in in enumerate(self.fiber_in):
                etype = f'({degree_in},{degree_out})'

                x = inp[degree_in]

                x = batched_index_select(x, neighbor_indices, dim = 1)
                x = x.view(*x.shape[:3], to_order(degree_in) * m_in, 1)

                kernel_fn = self.kernel_unary[etype]
                edge_features = torch.cat((rel_dist, edges), dim = -1) if exists(edges) else rel_dist

                output_chunk = None
                split_x = fast_split(x, splits, dim = 1)
                split_edge_features = fast_split(edge_features, splits, dim = 1)

                # process input, edges, and basis in chunks along the sequence dimension

                for x_chunk, edge_features, basis in zip(split_x, split_edge_features, split_basis):
                    kernel = kernel_fn(edge_features, basis = basis)
                    chunk = einsum('... o i, ... i c -> ... o c', kernel, x_chunk)
                    output_chunk = safe_cat(output_chunk, chunk, dim = 1)

                output = output + output_chunk

            if self.pool:
                output = masked_mean(output, neighbor_masks, dim = 2) if exists(neighbor_masks) else output.mean(dim = 2)

            leading_shape = x.shape[:2] if self.pool else x.shape[:3]
            output = output.view(*leading_shape, -1, to_order(degree_out))

            outputs[degree_out] = output

        if self.self_interaction:
            self_interact_out = self.self_interact(inp)
            outputs = residual_fn(outputs, self_interact_out)

        return outputs

class RadialFunc(nn.Module):
    def __init__(
        self,
        num_freq,
        in_dim,
        out_dim,
        edge_dim = None,
        mid_dim = 128
    ):
        super().__init__()
        self.num_freq = num_freq
        self.in_dim = in_dim
        self.mid_dim = mid_dim
        self.out_dim = out_dim

        self.net = nn.Sequential(
            nn.Linear(default(edge_dim, 0) + 1, mid_dim),
            nn.GELU(),
            nn.LayerNorm(mid_dim),
            nn.Linear(mid_dim, mid_dim),
            nn.GELU(),
            nn.LayerNorm(mid_dim),
            nn.Linear(mid_dim, num_freq * in_dim * out_dim)
        )

    def forward(self, x):
        y = self.net(x)
        return rearrange(y, '... (o i f) -> ... o 1 i 1 f', i = self.in_dim, o = self.out_dim)

class PairwiseConv(nn.Module):
    def __init__(
        self,
        degree_in,
        nc_in,
        degree_out,
        nc_out,
        edge_dim = 0
    ):
        super().__init__()
        self.degree_in = degree_in
        self.degree_out = degree_out
        self.nc_in = nc_in
        self.nc_out = nc_out

        self.num_freq = to_order(min(degree_in, degree_out))
        self.d_out = to_order(degree_out)
        self.edge_dim = edge_dim

        self.rp = RadialFunc(self.num_freq, nc_in, nc_out, edge_dim)

    def forward(self, feat, basis):
        R = self.rp(feat)
        B = basis[f'{self.degree_in},{self.degree_out}']

        out_shape = (*R.shape[:3], self.d_out * self.nc_out, -1)

        # torch.sum(R * B, dim = -1) is too memory intensive
        # needs to be chunked to reduce peak memory usage

        out = 0
        for i in range(R.shape[-1]):
            out += R[..., i] * B[..., i]

        out = rearrange(out, 'b n h s ... -> (b n h s) ...')

        # reshape and out
        return out.view(*out_shape)

# feed forwards

@beartype
class FeedForward(nn.Module):
    def __init__(
        self,
        fiber: Tuple[int, ...],
        fiber_out: Optional[Tuple[int, ...]] = None,
        mult = 4
    ):
        super().__init__()
        self.fiber = fiber

        fiber_hidden = tuple(dim * mult for dim in fiber)

        dim_gate = sum(fiber_hidden[1:]) # sum of dimensions of type 1+, gated by sigmoid of type 0 in paper as nonlinearity
        project_in_fiber_hidden = list(fiber_hidden)
        project_in_fiber_hidden[0] += dim_gate
        project_in_fiber_hidden = tuple(project_in_fiber_hidden)

        fiber_out = default(fiber_out, fiber)

        self.prenorm     = Norm(fiber)
        self.project_in  = Linear(fiber, project_in_fiber_hidden)
        self.gate        = Gate(project_in_fiber_hidden)
        self.project_out = Linear(fiber_hidden, fiber_out)

    def forward(self, features):
        outputs = self.prenorm(features)

        outputs = self.project_in(outputs)
        outputs = self.gate(outputs)
        outputs = self.project_out(outputs)
        return outputs

# attention

@beartype
class Attention(nn.Module):
    def __init__(
        self,
        fiber: Tuple[int, ...],
        dim_head: Union[int, Tuple[int, ...]] = 64,
        heads: Union[int, Tuple[int, ...]] = 8,
        attend_self = False,
        edge_dim = None,
        splits = 4
    ):
        super().__init__()
        num_degrees = len(fiber)

        dim_head = cast_tuple(dim_head, num_degrees)
        assert len(dim_head) == num_degrees

        heads = cast_tuple(heads, num_degrees)
        assert len(heads) == num_degrees

        hidden_fiber = tuple(dim * head for dim, head in zip(dim_head, heads))

        self.scale = tuple(dim ** -0.5 for dim in dim_head)
        self.heads = heads

        self.prenorm = Norm(fiber)

        self.to_q = Linear(fiber, hidden_fiber)
        self.to_v = Conv(fiber, hidden_fiber, edge_dim = edge_dim, pool = False, self_interaction = False, splits = splits)
        self.to_k = Conv(fiber, hidden_fiber, edge_dim = edge_dim, pool = False, self_interaction = False, splits = splits)

        self.to_out = Linear(hidden_fiber, fiber)

        self.attend_self = attend_self
        if attend_self:
            self.to_self_k = Linear(fiber, hidden_fiber)
            self.to_self_v = Linear(fiber, hidden_fiber)


    def forward(self, features, edge_info, rel_dist, basis, pos_emb = None, mask = None):
        attend_self = self.attend_self
        device, dtype = get_tensor_device_and_dtype(features)
        neighbor_indices, neighbor_mask, edges = edge_info

        if exists(neighbor_mask):
            neighbor_mask = rearrange(neighbor_mask, 'b i j -> b 1 i j')

        features = self.prenorm(features)

        queries = self.to_q(features)
        keys    = self.to_k(features, edge_info, rel_dist, basis)
        values  = self.to_v(features, edge_info, rel_dist, basis)

        if attend_self:
            self_keys, self_values = self.to_self_k(features), self.to_self_v(features)

        outputs = {}

        for degree, h, scale in zip(features.keys(), self.heads, self.scale):
            q, k, v = map(lambda t: t[degree], (queries, keys, values))

            q = rearrange(q, 'b i (h d) m -> b h i d m', h = h)
            k, v = map(lambda t: rearrange(t, 'b i j (h d) m -> b h i j d m', h = h), (k, v))

            if attend_self:
                self_k, self_v = map(lambda t: t[degree], (self_keys, self_values))
                self_k, self_v = map(lambda t: rearrange(t, 'b n (h d) m -> b h n 1 d m', h = h), (self_k, self_v))
                k = torch.cat((self_k, k), dim = 3)
                v = torch.cat((self_v, v), dim = 3)

            sim = einsum('b h i d m, b h i j d m -> b h i j', q, k) * scale

            if exists(neighbor_mask):
                num_left_pad = sim.shape[-1] - neighbor_mask.shape[-1]
                mask = F.pad(neighbor_mask, (num_left_pad, 0), value = True)
                sim = sim.masked_fill(~mask, -torch.finfo(sim.dtype).max)

            attn = sim.softmax(dim = -1)
            out = einsum('b h i j, b h i j d m -> b h i d m', attn, v)
            outputs[degree] = rearrange(out, 'b h n d m -> b n (h d) m')

        return self.to_out(outputs)

# main class

@beartype
class Equiformer(nn.Module):
    def __init__(
        self,
        *,
        dim: Union[int, Tuple[int, ...]],
        dim_in: Optional[Union[int, Tuple[int, ...]]] = None,
        num_degrees = 2,
        input_degrees = 1,
        heads: Union[int, Tuple[int, ...]] = 8,
        dim_head: Union[int, Tuple[int, ...]] = 24,
        depth = 2,
        valid_radius = 1e5,
        num_neighbors = float('inf'),
        reduce_dim_out = False,
        num_tokens = None,
        num_positions = None,
        num_edge_tokens = None,
        edge_dim = None,
        attend_self = True,
        differentiable_coors = False,
        splits = 4,
        linear_out = True
    ):
        super().__init__()

        # decide hidden dimensions for all types

        self.dim = cast_tuple(dim, num_degrees)
        assert len(self.dim) == num_degrees

        self.num_degrees = len(self.dim)

        # decide input dimensions for all types

        dim_in = default(dim_in, (self.dim[0],))
        self.dim_in = cast_tuple(dim_in, input_degrees)
        assert len(self.dim_in) == input_degrees

        self.input_degrees = len(self.dim_in)

        # token embedding

        type0_feat_dim = self.dim_in[0]
        self.type0_feat_dim = type0_feat_dim

        self.token_emb = nn.Embedding(num_tokens, type0_feat_dim) if exists(num_tokens) else None

        # positional embedding

        self.num_positions = num_positions
        self.pos_emb = nn.Embedding(num_positions, type0_feat_dim) if exists(num_positions) else None

        # edges

        assert not (exists(num_edge_tokens) and not exists(edge_dim)), 'edge dimension (edge_dim) must be supplied if SE3 transformer is to have edge tokens'

        self.edge_emb = nn.Embedding(num_edge_tokens, edge_dim) if exists(num_edge_tokens) else None
        self.has_edges = exists(edge_dim) and edge_dim > 0

        # whether to differentiate through basis, needed for alphafold2

        self.differentiable_coors = differentiable_coors

        # neighbors hyperparameters

        self.valid_radius = valid_radius
        self.num_neighbors = num_neighbors

        # define fibers and dimensionality

        conv_kwargs = dict(edge_dim = edge_dim, splits = splits)

        # main network

        self.conv_in  = Conv(self.dim_in, self.dim, **conv_kwargs)

        # trunk

        self.attend_self = attend_self

        self.layers = nn.ModuleList([])

        for ind in range(depth):
            self.layers.append(nn.ModuleList([
                Attention(self.dim, heads = heads, dim_head = dim_head, attend_self = attend_self, edge_dim = edge_dim, splits = splits),
                FeedForward(self.dim)
            ]))

        # out

        self.norm = Norm(self.dim)

        proj_out_klass = Linear if linear_out else FeedForward

        self.ff_out = proj_out_klass(self.dim, (1,) * self.num_degrees) if reduce_dim_out else None

    def forward(
        self,
        feats,
        coors,
        mask = None,
        adj_mat = None,
        edges = None,
        return_type = None,
        return_pooled = False,
        neighbor_mask = None,
    ):
        _mask = mask

        if exists(self.token_emb):
            feats = self.token_emb(feats)

        if exists(self.pos_emb):
            assert feats.shape[1] <= self.num_positions, 'feature sequence length must be less than the number of positions given at init'
            feats = feats + self.pos_emb(torch.arange(feats.shape[1], device = feats.device))

        assert not (self.has_edges and not exists(edges)), 'edge embedding (num_edge_tokens & edge_dim) must be supplied if one were to train on edge types'

        if torch.is_tensor(feats):
            feats = rearrange(feats, '... -> ... 1')
            feats = {0: feats}

        b, n, d, *_, device = *feats[0].shape, feats[0].device

        assert d == self.type0_feat_dim, f'feature dimension {d} must be equal to dimension given at init {self.type0_feat_dim}'
        assert set(map(int, feats.keys())) == set(range(self.input_degrees)), f'input must have {self.input_degrees} degree'

        num_degrees, neighbors, valid_radius = self.num_degrees, self.num_neighbors, self.valid_radius

        # se3 transformer by default cannot have a node attend to itself

        exclude_self_mask = rearrange(~torch.eye(n, dtype = torch.bool, device = device), 'i j -> 1 i j')
        remove_self = lambda t: t.masked_select(exclude_self_mask).reshape(b, n, n - 1)
        get_max_value = lambda t: torch.finfo(t.dtype).max

        # exclude edge of token to itself

        indices = repeat(torch.arange(n, device = device), 'j -> b i j', b = b, i = n)
        rel_pos  = rearrange(coors, 'b n d -> b n 1 d') - rearrange(coors, 'b n d -> b 1 n d')

        indices = indices.masked_select(exclude_self_mask).reshape(b, n, n - 1)
        rel_pos = rel_pos.masked_select(exclude_self_mask[..., None]).reshape(b, n, n - 1, 3)

        if exists(mask):
            mask = rearrange(mask, 'b i -> b i 1') * rearrange(mask, 'b j -> b 1 j')
            mask = mask.masked_select(exclude_self_mask).reshape(b, n, n - 1)

        if exists(edges):
            if exists(self.edge_emb):
                edges = self.edge_emb(edges)

            edges = edges.masked_select(exclude_self_mask[..., None]).reshape(b, n, n - 1, -1)

        rel_dist = rel_pos.norm(dim = -1)

        # rel_dist gets modified using adjacency or neighbor mask

        modified_rel_dist = rel_dist.clone()
        max_value = get_max_value(modified_rel_dist) # for masking out nodes from being considered as neighbors

        # neighbors

        if exists(neighbor_mask):
            neighbor_mask = remove_self(neighbor_mask)

            max_neighbors = neighbor_mask.sum(dim = -1).max().item()
            if max_neighbors > neighbors:
                print(f'neighbor_mask shows maximum number of neighbors as {max_neighbors} but specified number of neighbors is {neighbors}')

            modified_rel_dist = modified_rel_dist.masked_fill(~neighbor_mask, max_value)

        # if number of local neighbors by distance is set to 0, then only fetch the sparse neighbors defined by adjacency matrix

        if neighbors == 0:
            valid_radius = 0

        # get neighbors and neighbor mask, excluding self

        neighbors = int(min(neighbors, n - 1))
        total_neighbors = neighbors

        assert total_neighbors > 0, 'you must be fetching at least 1 neighbor'

        total_neighbors = int(min(total_neighbors, n - 1)) # make sure total neighbors does not exceed the length of the sequence itself

        dist_values, nearest_indices = modified_rel_dist.topk(total_neighbors, dim = -1, largest = False)
        neighbor_mask = dist_values <= valid_radius

        neighbor_rel_dist = batched_index_select(rel_dist, nearest_indices, dim = 2)
        neighbor_rel_pos = batched_index_select(rel_pos, nearest_indices, dim = 2)
        neighbor_indices = batched_index_select(indices, nearest_indices, dim = 2)

        if exists(mask):
            neighbor_mask = neighbor_mask & batched_index_select(mask, nearest_indices, dim = 2)

        if exists(edges):
            edges = batched_index_select(edges, nearest_indices, dim = 2)

        # calculate basis

        basis = get_basis(neighbor_rel_pos, num_degrees - 1, differentiable = self.differentiable_coors)

        # main logic

        edge_info = (neighbor_indices, neighbor_mask, edges)
        x = feats

        # project in

        x = self.conv_in(x, edge_info, rel_dist = neighbor_rel_dist, basis = basis)

        # transformer layers

        attn_kwargs = dict(
            edge_info = edge_info,
            rel_dist = neighbor_rel_dist,
            basis = basis,
            mask = _mask
        )

        for attn, ff in self.layers:
            x = residual_fn(attn(x, **attn_kwargs), x)
            x = residual_fn(ff(x), x)

        # norm

        x = self.norm(x)

        # reduce dim if specified

        if exists(self.ff_out):
            x = self.ff_out(x)
            x = {k: rearrange(v, '... 1 c -> ... c') for k, v in x.items()}

        if return_pooled:
            mask_fn = (lambda t: masked_mean(t, _mask, dim = 1)) if exists(_mask) else (lambda t: t.mean(dim = 1))
            x = {k: mask_fn(v) for k, v in x.items()}

        # just return type 0 and type 1 features, reduced or not

        type0, type1 = x[0], x[1]

        type0 = rearrange(type0, '... 1 -> ...') # for type 0, just squeeze out the last dimension

        return Return(type0, type1)
