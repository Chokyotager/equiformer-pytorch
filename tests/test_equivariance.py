import pytest
import torch
from equiformer_pytorch.equiformer_pytorch import Equiformer
from equiformer_pytorch.irr_repr import rot
from equiformer_pytorch.utils import torch_default_dtype

@pytest.mark.skip()
def test_transformer():
    model = Equiformer()

    feats = torch.randn(1, 32, 64)
    coors = torch.randn(1, 32, 3)
    mask  = torch.ones(1, 32).bool()

    out = model(feats, coors, mask, return_type = 0)
    assert out.shape == (1, 32, 64), 'output must be of the right shape'

@pytest.mark.skip()
def test_equivariance():
    model = Equiformer()

    feats = torch.randn(1, 32, 64)
    coors = torch.randn(1, 32, 3)
    mask  = torch.ones(1, 32).bool()

    R   = rot(15, 0, 45)
    out1 = model(feats, coors @ R, mask, return_type = 1)
    out2 = model(feats, coors, mask, return_type = 1) @ R

    diff = (out1 - out2).max()
    assert diff < 1e-4, 'is not equivariant'
