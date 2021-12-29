r"""Flows and parametric distributions"""


import nflows.distributions as D
import nflows.transforms as T
import torch
import torch.nn as nn
import torch.nn.functional as F

from functools import cached_property

from nflows.flows import Flow
from torch import Tensor


class NormalizingFlow(Flow):
    r"""Normalizing Flow

    (x, y) -> log p(x | y)

    Args:
        base: The base distribution.
        transforms: A list of (learnable) conditional transforms.
    """

    def __init__(self, base: D.Distribution, transforms: list[T.Transform]):
        super().__init__(
            T.CompositeTransform(transforms),
            base
        )

    def log_prob(self, x: Tensor, y: Tensor) -> Tensor:
        r""" log p(x | y) """

        return super().log_prob(x, y)

    def sample(self, y: Tensor, shape: torch.Size = ()) -> Tensor:
        r""" x ~ p(x | y) """

        size = torch.Size(shape).numel()

        x = super()._sample(size, y[None])
        x = x.view(y.shape[:-1] + shape + x.shape[-1:])

        return x


class MAF(NormalizingFlow):
    r"""Masked Autoregressive Flow (MAF)

    Args:
        x_size: The input size.
        y_size: The context size.
        num_transforms: The number of transforms.
        moments: The input moments (mu, sigma) for standardization.

    References:
        [1] Masked Autoregressive Flow for Density Estimation
        (Papamakarios et al., 2017)
        https://arxiv.org/abs/1705.07057
    """

    def __init__(
        self,
        x_size: int,
        y_size: int,
        architecture: str = 'affine',  # ['PRQ', 'UMNN']
        num_transforms: int = 5,
        moments: tuple[Tensor, Tensor] = None,
        **kwargs,
    ):
        kwargs.setdefault('hidden_features', 64)
        kwargs.setdefault('num_blocks', 2)
        kwargs.setdefault('use_residual_blocks', False)
        kwargs.setdefault('use_batch_norm', False)
        kwargs.setdefault('activation', F.relu)

        if architecture == 'PRQ':
            kwargs['tails'] = 'linear'
            kwargs.setdefault('num_bins', 8)
            kwargs.setdefault('tail_bound', 1.)

            tf = T.MaskedPiecewiseRationalQuadraticAutoregressiveTransform
        elif architecture == 'UMNN':
            kwargs.setdefault('integrand_net_layers', [64, 64, 64])
            kwargs.setdefault('cond_size', 32)
            kwargs.setdefault('nb_steps', 32)

            tf = T.MaskedUMNNAutoregressiveTransform
        else:  # architecture == 'affine'
            tf = T.MaskedAffineAutoregressiveTransform

        transforms = []

        if moments is not None:
            shift, scale = moments
            transforms.append(T.PointwiseAffineTransform(-shift / scale, 1 / scale))

        for _ in range(num_transforms):
            transforms.extend([
                tf(
                    features=x_size,
                    context_features=y_size,
                    **kwargs,
                ),
                T.RandomPermutation(features=x_size),
            ])

        base = D.StandardNormal((x_size,))

        super().__init__(base, transforms)
