r"""Datasets and data loaders."""

__all__ = ['JointLoader', 'H5Dataset']

import h5py
import numpy as np
import random
import torch

from bisect import bisect
from contextlib import ExitStack
from numpy import ndarray as Array
from pathlib import Path
from torch import Tensor, Size
from torch.distributions import Distribution
from torch.utils.data import DataLoader, Dataset, IterableDataset
from tqdm import tqdm
from typing import *


class IterableJointDataset(IterableDataset):
    r"""Creates an iterable dataset of batched pairs :math:`(\theta, x)`."""

    def __init__(
        self,
        prior: Distribution,
        simulator: Callable,
        batch_shape: Size = (),
        numpy: bool = False,
    ):
        super().__init__()

        self.prior = prior
        self.simulator = simulator
        self.batch_shape = batch_shape
        self.numpy = numpy

    def __iter__(self) -> Iterator[Tuple[Tensor, Tensor]]:
        while True:
            theta = self.prior.sample(self.batch_shape)

            if self.numpy:
                x = self.simulator(theta.detach().cpu().numpy().astype(np.double))
                x = torch.from_numpy(x).to(theta)
            else:
                x = self.simulator(theta)

            yield theta, x


class JointLoader(DataLoader):
    r"""Creates an infinite data loader of batched pairs :math:`(\theta, x)` generated
    by a prior distribution :math:`p(\theta)` and a simulator.

    The simulator is a stochastic function taking (a vector of) parameters
    :math:`\theta`, in the form of a NumPy array or a PyTorch tensor, as input and
    returning an observation :math:`x` as output, which implicitly defines a
    likelihood distribution :math:`p(x | \theta)`. Together with the prior, they form
    a joint distribution :math:`p(\theta, x) = p(\theta) p(x | \theta)` from which
    pairs :math:`(\theta, x)` are independently drawn.

    Arguments:
        prior: A prior distribution :math:`p(\theta)`.
        simulator: A callable simulator.
        batch_size: The batch size of the generated pairs.
        vectorized: Whether the simulator accepts batched inputs or not.
        numpy: Whether the simulator requires NumPy or PyTorch inputs.
        kwargs: Keyword arguments passed to :class:`torch.utils.data.DataLoader`.

    Example:
        >>> loader = JointLoader(prior, simulator, numpy=True, num_workers=4)
        >>> for theta, x in loader:
        ...     theta, x = theta.cuda(), x.cuda()
        ...     something(theta, x)
    """

    def __init__(
        self,
        prior: Distribution,
        simulator: Callable,
        batch_size: int = 2**10,  # 1024
        vectorized: bool = False,
        numpy: bool = False,
        **kwargs,
    ):
        super().__init__(
            IterableJointDataset(
                prior,
                simulator,
                batch_shape=(batch_size,) if vectorized else (),
                numpy=numpy,
            ),
            batch_size=None if vectorized else batch_size,
            **kwargs,
        )


class H5Dataset(IterableDataset):
    r"""Creates an iterable dataset of pairs :math:`(\theta, x)` from HDF5 files.

    As it can be slow to load pairs from disk one by one, :class:`H5Dataset` implements
    a custom :meth:`__iter__` method that loads several contiguous chunks of pairs at
    once and shuffles their concatenation before yielding the pairs one by one,
    unless a batch size is provided.

    :class:`H5Dataset` also implements the :meth:`__len__` and :meth:`__getitem__`
    methods for convenience.

    Important:
        When using :class:`H5Dataset` with :class:`torch.utils.data.DataLoader`, it is
        recommended to disable the loader's automatic batching and provide the batch
        size to the dataset, as it significantly improves loading performances.
        In fact, unless using several workers or memory pinning, it is not even
        necessary to wrap the dataset in a :class:`torch.utils.data.DataLoader`.

    Arguments:
        files: HDF5 files containing pairs :math:`(\theta, x)`.
        batch_size: The size of the batches.
        chunk_size: The size of the contiguous chunks.
        chunk_step: The number of chunks loaded at once.
        shuffle: Whether the pairs are shuffled or not when iterating.

    Example:
        >>> dataset = H5Dataset('data.h5', batch_size=256, shuffle=True)
        >>> theta, x = dataset[0]
        >>> theta
        tensor([-0.1215, -1.3641,  0.7233, -1.2150, -1.9263])
        >>> for theta, x in dataset:
        ...     theta, x = theta.cuda(), x.cuda()
        ...     something(theta, x)
    """

    def __init__(
        self,
        *files: Union[str, Path],
        batch_size: int = None,
        chunk_size: int = 2**10,  # 1024
        chunk_step: str = 2**8,  # 256
        shuffle: bool = False,
    ):
        super().__init__()

        self.files = files

        with ExitStack() as stack:
            files = map(stack.enter_context, map(h5py.File, self.files))
            self.cumsizes = np.cumsum([len(f['x']) for f in files])

        self.batch_size = batch_size
        self.chunk_size = chunk_size
        self.chunk_step = chunk_step
        self.shuffle = shuffle

    def __len__(self) -> int:
        return self.cumsizes[-1]

    def __getitem__(self, i: int) -> Tuple[Tensor, Tensor]:
        i = i % len(self)
        j = bisect(self.cumsizes, i)
        if j > 0:
            i = i - self.cumsizes[j - 1]

        with h5py.File(self.files[j]) as f:
            theta, x = f['theta'][i], f['x'][i]

        return torch.from_numpy(theta), torch.from_numpy(x)

    def __iter__(self) -> Iterator[Tuple[Tensor, Tensor]]:
        with ExitStack() as stack:
            files = list(map(stack.enter_context, map(h5py.File, self.files)))

            chunks = torch.tensor([
                (i, j, j + self.chunk_size)
                for i, f in enumerate(files)
                for j in range(0, len(f['x']), self.chunk_size)
            ])

            if self.shuffle:
                order = torch.randperm(len(chunks))
                chunks = chunks[order]

            for slices in chunks.split(self.chunk_step):
                slices = sorted(slices.tolist())

                # Load
                theta = np.concatenate([files[i]['theta'][j:k] for i, j, k in slices])
                x = np.concatenate([files[i]['x'][j:k] for i, j, k in slices])

                theta, x = torch.from_numpy(theta), torch.from_numpy(x)

                # Shuffle
                if self.shuffle:
                    order = torch.randperm(len(x))
                    theta, x = theta[order], x[order]

                # Batch
                if self.batch_size is None:
                    yield from zip(theta, x)
                else:
                    yield from zip(
                        theta.split(self.batch_size),
                        x.split(self.batch_size),
                    )

    @staticmethod
    def store(
        pairs: Iterable[Tuple[Array, Array]],
        file: Union[str, Path],
        size: int,
        overwrite: bool = False,
        dtype: np.dtype = np.float32,
        **meta,
    ) -> None:
        r"""Creates an HDF5 file containing pairs :math:`(\theta, x)`.

        The sets of parameters :math:`\theta` are stored in a collection named
        :py:`'theta'` and the observations in a collection named :py:`'x'`.

        Arguments:
            pairs: An iterable over batched pairs :math:`(\theta, x)`.
            file: An HDF5 filename to store pairs in.
            size: The number of pairs to store.
            overwrite: Whether to overwrite existing files or not. If :py:`False`
                and the file already exists, the function raises an error.
            dtype: The data type to store pairs in.
            meta: Metadata to store in the file.

        Example:
            >>> loader = JointLoader(prior, simulator, batch_size=16)
            >>> H5Dataset.store(loader, 'data.h5', 4096)
            100%|██████████| 4096/4096 [01:35<00:00, 42.69pair/s]
        """

        # Pairs
        pairs = iter(pairs)

        # File
        file = Path(file)
        file.parent.mkdir(parents=True, exist_ok=True)

        with h5py.File(file, 'w' if overwrite else 'w-') as f:
            ## Attributes
            f.attrs.update(meta)

            ## Datasets
            theta, x = map(np.asarray, next(pairs))

            f.create_dataset('theta', (size,) + theta.shape[1:], dtype=dtype)
            f.create_dataset('x', (size,) + x.shape[1:], dtype=dtype)

            ## Store
            with tqdm(total=size, unit='pair') as tq:
                i = 0

                while True:
                    j = min(i + theta.shape[0], size)

                    f['theta'][i:j] = theta[: j - i]
                    f['x'][i:j] = x[: j - i]

                    tq.update(j - i)

                    if j < size:
                        i = j

                        try:
                            theta, x = map(np.asarray, next(pairs))
                        except StopIteration:
                            break
                    else:
                        break
