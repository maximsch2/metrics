# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import pickle
import sys
from functools import partial
from typing import Callable

import numpy as np
import pytest
import torch
from torch import Tensor, tensor
from torch.multiprocessing import Pool, set_start_method

from torchmetrics import Metric

try:
    set_start_method("spawn")
except RuntimeError:
    pass

NUM_PROCESSES = 2
NUM_BATCHES = 10
BATCH_SIZE = 32
NUM_CLASSES = 5
EXTRA_DIM = 3
THRESHOLD = 0.5


def setup_ddp(rank, world_size):
    """ Setup ddp environment """
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "8088"

    if torch.distributed.is_available() and sys.platform not in ("win32", "cygwin"):
        torch.distributed.init_process_group("gloo", rank=rank, world_size=world_size)


def _assert_allclose(pl_result, sk_result, atol: float = 1e-8):
    """ Utility function for recursively asserting that two results are within
        a certain tolerance
    """
    # single output compare
    if isinstance(pl_result, Tensor):
        assert np.allclose(pl_result.numpy(), sk_result, atol=atol, equal_nan=True)
    # multi output compare
    elif isinstance(pl_result, (tuple, list)):
        for pl_res, sk_res in zip(pl_result, sk_result):
            _assert_allclose(pl_res, sk_res, atol=atol)
    else:
        raise ValueError('Unknown format for comparison')


def _assert_tensor(pl_result):
    """ Utility function for recursively checking that some input only consist of
        torch tensors
    """
    if isinstance(pl_result, (list, tuple)):
        for plr in pl_result:
            _assert_tensor(plr)
    else:
        assert isinstance(pl_result, Tensor)


def _class_test(
    rank: int,
    worldsize: int,
    preds: Tensor,
    target: Tensor,
    metric_class: Metric,
    sk_metric: Callable,
    dist_sync_on_step: bool,
    metric_args: dict = None,
    check_dist_sync_on_step: bool = True,
    check_batch: bool = True,
    atol: float = 1e-8,
):
    """Utility function doing the actual comparison between lightning class metric
    and reference metric.

    Args:
        rank: rank of current process
        worldsize: number of processes
        preds: torch tensor with predictions
        target: torch tensor with targets
        metric_class: lightning metric class that should be tested
        sk_metric: callable function that is used for comparison
        dist_sync_on_step: bool, if true will synchronize metric state across
            processes at each ``forward()``
        metric_args: dict with additional arguments used for class initialization
        check_dist_sync_on_step: bool, if true will check if the metric is also correctly
            calculated per batch per device (and not just at the end)
        check_batch: bool, if true will check if the metric is also correctly
            calculated across devices for each batch (and not just at the end)
    """
    if not metric_args:
        metric_args = {}
    # Instanciate lightning metric
    metric = metric_class(
        compute_on_step=check_dist_sync_on_step or check_batch,
        dist_sync_on_step=dist_sync_on_step,
        **metric_args
    )

    # verify metrics work after being loaded from pickled state
    pickled_metric = pickle.dumps(metric)
    metric = pickle.loads(pickled_metric)

    for i in range(rank, NUM_BATCHES, worldsize):
        batch_result = metric(preds[i], target[i])

        if metric.dist_sync_on_step and check_dist_sync_on_step and rank == 0:
            ddp_preds = torch.cat([preds[i + r] for r in range(worldsize)])
            ddp_target = torch.cat([target[i + r] for r in range(worldsize)])
            sk_batch_result = sk_metric(ddp_preds, ddp_target)
            _assert_allclose(batch_result, sk_batch_result, atol=atol)
        elif check_batch and not metric.dist_sync_on_step:
            # assert for batch
            sk_batch_result = sk_metric(preds[i], target[i])
            _assert_allclose(batch_result, sk_batch_result, atol=atol)

    # check on all batches on all ranks
    result = metric.compute()
    _assert_tensor(result)

    total_preds = torch.cat([preds[i] for i in range(NUM_BATCHES)])
    total_target = torch.cat([target[i] for i in range(NUM_BATCHES)])
    sk_result = sk_metric(total_preds, total_target)

    # assert after aggregation
    _assert_allclose(result, sk_result, atol=atol)


def _functional_test(
    preds: Tensor,
    target: Tensor,
    metric_functional: Callable,
    sk_metric: Callable,
    metric_args: dict = None,
    atol: float = 1e-8,
):
    """Utility function doing the actual comparison between lightning functional metric
    and reference metric.

    Args:
        preds: torch tensor with predictions
        target: torch tensor with targets
        metric_functional: lightning metric functional that should be tested
        sk_metric: callable function that is used for comparison
        metric_args: dict with additional arguments used for class initialization
    """
    if not metric_args:
        metric_args = {}
    metric = partial(metric_functional, **metric_args)

    for i in range(NUM_BATCHES):
        lightning_result = metric(preds[i], target[i])
        sk_result = sk_metric(preds[i], target[i])

        # assert its the same
        _assert_allclose(lightning_result, sk_result, atol=atol)


class MetricTester:
    """Class used for efficiently run alot of parametrized tests in ddp mode.
    Makes sure that ddp is only setup once and that pool of processes are
    used for all tests.

    All tests should subclass from this and implement a new method called
        `test_metric_name`
    where the method `self.run_metric_test` is called inside.
    """

    atol = 1e-8

    def setup_class(self):
        """Setup the metric class. This will spawn the pool of workers that are
        used for metric testing and setup_ddp
        """

        self.poolSize = NUM_PROCESSES
        self.pool = Pool(processes=self.poolSize)
        self.pool.starmap(setup_ddp, [(rank, self.poolSize) for rank in range(self.poolSize)])

    def teardown_class(self):
        """ Close pool of workers """
        self.pool.close()
        self.pool.join()

    def run_functional_metric_test(
        self,
        preds: Tensor,
        target: Tensor,
        metric_functional: Callable,
        sk_metric: Callable,
        metric_args: dict = None,
    ):
        """Main method that should be used for testing functions. Call this inside
        testing method

        Args:
            preds: torch tensor with predictions
            target: torch tensor with targets
            metric_functional: lightning metric class that should be tested
            sk_metric: callable function that is used for comparison
            metric_args: dict with additional arguments used for class initialization
        """
        _functional_test(
            preds=preds,
            target=target,
            metric_functional=metric_functional,
            sk_metric=sk_metric,
            metric_args=metric_args,
            atol=self.atol,
        )

    def run_class_metric_test(
        self,
        ddp: bool,
        preds: Tensor,
        target: Tensor,
        metric_class: Metric,
        sk_metric: Callable,
        dist_sync_on_step: bool,
        metric_args: dict = None,
        check_dist_sync_on_step: bool = True,
        check_batch: bool = True,
    ):
        """Main method that should be used for testing class. Call this inside testing
        methods.

        Args:
            ddp: bool, if running in ddp mode or not
            preds: torch tensor with predictions
            target: torch tensor with targets
            metric_class: lightning metric class that should be tested
            sk_metric: callable function that is used for comparison
            dist_sync_on_step: bool, if true will synchronize metric state across
                processes at each ``forward()``
            metric_args: dict with additional arguments used for class initialization
            check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                calculated per batch per device (and not just at the end)
            check_batch: bool, if true will check if the metric is also correctly
                calculated across devices for each batch (and not just at the end)
        """
        if not metric_args:
            metric_args = {}
        if ddp:
            if sys.platform == "win32":
                pytest.skip("DDP not supported on windows")

            self.pool.starmap(
                partial(
                    _class_test,
                    preds=preds,
                    target=target,
                    metric_class=metric_class,
                    sk_metric=sk_metric,
                    dist_sync_on_step=dist_sync_on_step,
                    metric_args=metric_args,
                    check_dist_sync_on_step=check_dist_sync_on_step,
                    check_batch=check_batch,
                    atol=self.atol,
                ),
                [(rank, self.poolSize) for rank in range(self.poolSize)],
            )
        else:
            _class_test(
                0,
                1,
                preds=preds,
                target=target,
                metric_class=metric_class,
                sk_metric=sk_metric,
                dist_sync_on_step=dist_sync_on_step,
                metric_args=metric_args,
                check_dist_sync_on_step=check_dist_sync_on_step,
                check_batch=check_batch,
                atol=self.atol,
            )


class DummyMetric(Metric):
    name = "Dummy"

    def __init__(self):
        super().__init__()
        self.add_state("x", tensor(0.0), dist_reduce_fx=None)

    def update(self):
        pass

    def compute(self):
        pass


class DummyListMetric(Metric):
    name = "DummyList"

    def __init__(self):
        super().__init__()
        self.add_state("x", list(), dist_reduce_fx=None)

    def update(self):
        pass

    def compute(self):
        pass


class DummyMetricSum(DummyMetric):

    def update(self, x):
        self.x += x

    def compute(self):
        return self.x


class DummyMetricDiff(DummyMetric):

    def update(self, y):
        self.x -= y

    def compute(self):
        return self.x
