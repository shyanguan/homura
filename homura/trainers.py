from abc import ABCMeta, abstractmethod
from functools import partial as Partial
from types import MethodType
from typing import Callable, Iterable, Dict, Mapping, Tuple, Optional, Any, List

import torch
from torch import nn, Tensor
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler as Scheduler
from torch.utils.data import DataLoader, DistributedSampler
from tqdm import tqdm

from homura import is_distributed
from homura.liblog import get_logger, _set_tqdm_print
from .metrics import accuracy
from .reporters import _ReporterBase, TQDMReporter, ReporterList
from .utils._vocabulary import *
from .utils.containers import TensorTuple, StepDict
from .utils.environment import get_global_rank, get_local_rank, is_horovod_available
from .utils.mixin import StateDictMixIn

__all__ = ["TrainerBase", "SupervisedTrainer"]


class TrainerBase(StateDictMixIn, metaclass=ABCMeta):

    def __init__(self,
                 model: nn.Module or Dict[str, nn.Module],
                 optimizer: Optional[Partial or Optimizer or Dict[str, Optimizer]],
                 loss_f: Optional[Callable or Dict[str, Callable]] = None,
                 *,
                 reporters: Optional[List[_ReporterBase]] = None,
                 scheduler: Optional[Partial or Scheduler or Dict[str, Scheduler]] = None,
                 update_scheduler_by_epoch: bool = True,
                 device: Optional[torch.device or str] = None,
                 verb: bool = True,
                 use_cudnn_benchmark: bool = True,
                 use_cuda_nonblocking: bool = False,
                 use_horovod: bool = False,
                 logger=None,
                 use_sync_bn: bool = False,
                 tqdm_ncols: int = 80,
                 **kwargs):

        if kwargs.get("callbacks"):
            raise DeprecationWarning("callback is deprecated, if you need, use homura before v2020.8")

        self.logger = logger or get_logger(__name__)

        self.device = device or (torch.device(GPU) if torch.cuda.is_available() else torch.device(CPU))

        # setup for distributed
        self._use_sync_bn = use_sync_bn
        if is_distributed():
            if self._use_sync_bn:
                model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
                self.logger.info("BNs of model are converted to nn.SyncBatchNorm")

            rank = get_local_rank()
            torch.cuda.set_device(rank)
            if get_global_rank() > 0:
                # to avoid overwriting
                verb = False

        # setup model
        if isinstance(model, nn.Module):
            self.model = model
        elif isinstance(model, dict):
            self.model = nn.ModuleDict(model)
        else:
            raise TypeError(f"Unknown type for `model`. Expected nn.Module or Dict[str, Module], but got {type(model)}")

        if GPU in str(self.device):
            self.model.to(self.device)
            torch.backends.cudnn.benchmark = use_cudnn_benchmark
            self._cuda_nonblocking = use_cuda_nonblocking
            self.logger.debug(f"cuda: True, cudnn.benchmark: {use_cudnn_benchmark}, "
                              f"cuda.nonblocking: {use_cuda_nonblocking}")
        else:
            self._cuda_nonblocking = False
            # usually, this is not expected
            self.logger.info(f"cuda: False (torch.cuda.is_available()={torch.cuda.is_available()})")

        if not use_horovod and is_distributed():
            self.model = nn.parallel.DistributedDataParallel(self.model, device_ids=[rank])

        # self.accessible_model is useful for e.g., checkpointing
        if isinstance(self.model, nn.parallel.DistributedDataParallel) or isinstance(self.model, nn.DataParallel):
            self.accessible_model = self.model.module
        else:
            self.accessible_model = self.model

        self.loss_f = loss_f
        self._verb = verb

        # setup optimizer and scheduler
        self.optimizer = optimizer
        self.scheduler = scheduler
        self._update_scheduler_by_epoch = update_scheduler_by_epoch
        self.set_optimizer()
        self.set_scheduler()

        if use_horovod:
            if not is_horovod_available():
                raise RuntimeError("horovod is not available!")
            import horovod.torch as hvd

            hvd.broadcast_parameters(self.model.state_dict(), root_rank=0)
            hvd.broadcast_optimizer_state(self.optimizer, root_rank=0)
            self.optimizer = hvd.DistributedOptimizer(self.optimizer,
                                                      named_parameters=self.model.named_parameters())

        reporters = reporters or []
        if not any([isinstance(rep, TQDMReporter) for rep in reporters]):
            # at least, TQDMReporter is used
            reporters.append(TQDMReporter(ncols=tqdm_ncols))
        self.reporter = ReporterList(reporters)

        # called via property
        # _step and _epoch are set to -1 because they are incremented before each iteration and epoch
        self._step = -1
        self._epoch = -1
        self._is_train = True

        # to nest, leave=False (https://github.com/tqdm/tqdm/blob/master/examples/simple_examples.py#L19)
        self._tqdm = lambda x: x
        if verb:
            self._tqdm = Partial(tqdm, ncols=tqdm_ncols, leave=False)
            _set_tqdm_print()

        for k, v in kwargs.items():
            if hasattr(self, k):
                raise AttributeError(f"{self} already has {k}")
            if torch.is_tensor(v):
                v = v.to(self.device)
            if isinstance(v, nn.Module):
                v.to(self.device)
            setattr(self, k, v)
            self.logger.debug(f"trainer sets {k} as a new attribute")

    @property
    def step(self
             ) -> int:
        return self._step

    @property
    def epoch(self
              ) -> int:
        return self._epoch

    @property
    def is_train(self
                 ) -> bool:
        return self._is_train

    @property
    def history(self
                ) -> Dict[str, Dict[str, List[float]]]:
        raise NotImplementedError

    @abstractmethod
    def iteration(self,
                  data: Tuple[Tensor, ...]
                  ) -> None:
        # Iteration
        pass

    def override_iteration(self,
                           new_iteration: Callable[[Tuple], None]
                           ) -> None:
        """ Override iteration method ::

            def new_iteration(trainer, data):
                input, target = data
                ...
                results.loss = loss
                return results
            trainer.update_iteration(new_iteration)

        :param new_iteration:
        :return:
        """
        setattr(self, "iteration", MethodType(new_iteration, self))
        self.logger.debug("Override iteration")

    def epoch_range(self,
                    epoch: int):
        tqdm_reporter = [rep for rep in self.reporter.reporters if isinstance(rep, TQDMReporter)][0]
        tqdm_reporter.set_iterator(range(epoch))
        return tqdm_reporter

    def _iteration(self,
                   data: Tuple[Tensor, ...],
                   mode: str
                   ) -> None:
        """ Iteration level training loop

        :param data: should be TensorTuple
        :param mode: train, test or val
        :return:
        """

        data = self.data_preprocess(data)
        self.iteration(data)
        if self.is_train and self.scheduler is not None and not self._update_scheduler_by_epoch:
            self.scheduler.step()

    def __enter__(self):
        """

        >>> with TrainerBase(...) as trainer:
        >>>     trainer.train(...)

        """

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exit()

    def _loop(self,
              data_loader: Iterable or DataLoader,
              mode: str
              ) -> None:

        for data in self._tqdm(data_loader):
            if self.is_train:
                # increment step here for `callbacks`
                self._step += 1
            self._iteration(data, mode)

        self.reporter.report(self.epoch, mode)
        self.logger.debug(f"epoch {self.epoch} finished")

    def data_preprocess(self,
                        data: Tuple[Tensor, ...]
                        ) -> Tuple[Tensor, ...]:
        """ Handle data sampled from dataloader before `iteration`.
        By default, `data` is expected to be a tuple of tensors,
        so wrap it with TensorTuple and send it to `self.device`

        """

        return TensorTuple(data).to(self.device, non_blocking=self._cuda_nonblocking)

    def train(self,
              data_loader: Iterable or DataLoader,
              mode: str = TRAIN
              ) -> None:
        """ Training the model for an epoch.

        :param data_loader:
        :param mode: Name of this loop. Default is `train`. Passed to callbacks.
        """

        self._is_train = True
        self._epoch += 1
        self.model.train()

        if hasattr(self.loss_f, "train"):
            self.loss_f.train()

        self._loop(data_loader, mode=mode)

        if self.scheduler is not None and self._update_scheduler_by_epoch:
            self.scheduler.step()

        # For distributed training
        if isinstance(data_loader, DataLoader) and isinstance(data_loader.sampler, DistributedSampler):
            data_loader.sampler.set_epoch(self.epoch)

    def test(self,
             data_loader: Iterable or DataLoader,
             mode: str = TEST
             ) -> None:
        """ Evaluate the model.

        :param data_loader:
        :param mode: Name of this loop. Default is `test`. Passed to callbacks.
        :return:
        """

        self._is_train = False
        self.model.eval()

        if hasattr(self.loss_f, "eval"):
            self.loss_f.eval()

        with torch.no_grad():
            self._loop(data_loader, mode=mode)

    def run(self,
            train_loader: Iterable or DataLoader,
            val_loaders: Iterable or DataLoader or Dict[str, Iterable or DataLoader],
            total_iterations: int,
            val_intervals: int
            ) -> None:

        """ Train the model for a given iterations. This module is almost equal to ::

            for ep in range(total_iterations):
                trainer.train(train_loader)
                for k, v in val_loaders.items():
                    trainer.test(v, k)

        :param train_loader:
        :param val_loaders:
        :param total_iterations:
        :param val_intervals:
        :return:
        """

        class ProxyLoader(object):
            def __init__(self, loader):
                self.loader = loader

            def __len__(self):
                return val_intervals

            def __iter__(self):
                counter = 0
                while True:
                    for data in self.loader:
                        if counter == val_intervals:
                            return  # from python 3.7, this is valid
                        yield data
                        counter += 1

        train_loader = ProxyLoader(train_loader)
        if not isinstance(val_loaders, Dict) and (isinstance(val_loaders, Iterable) or
                                                  isinstance(val_loaders, DataLoader)):
            val_loaders = {'val': val_loaders}

        for ep in range(total_iterations // val_intervals):
            self.train(train_loader)
            if isinstance(train_loader.loader, DataLoader) \
                    and isinstance(train_loader.loader.sampler, DistributedSampler):
                train_loader.loader.sampler.set_epoch(self.epoch)
            for name, loader in val_loaders.items():
                self.test(loader, name)

    def exit(self):
        self.reporter.exit()

    def set_optimizer(self
                      ) -> None:
        """ Set optimizer(s) for model(s). You can override as::

            class YourTrainer(TrainerBase):
                def set_optimizer(self):
                    self.optimizer = torch.optim.SGD(self.model.parameters())

        :return:
        """

        optimizer = self.optimizer
        if isinstance(optimizer, Optimizer) or optimizer is None:
            self.optimizer = optimizer

        elif isinstance(optimizer, Partial):
            if not issubclass(optimizer.func, Optimizer):
                raise TypeError(f"`optimizer.func` is expected to be subclass of `Optimizer`"
                                f" but got {type(optimizer.func)}")
            self.optimizer = optimizer(self.model.parameters())

        elif isinstance(optimizer, dict):
            if not isinstance(self.model, nn.ModuleDict):
                raise TypeError("When `optimizer` is `dict`, `model` also needs to be `dict` or `nn.ModuleDict`")

            if isinstance(list(optimizer.values())[0], Partial):
                optimizer = {k: v(self.model[k].parameters()) for k, v in optimizer.items() if v is not None}
            self.optimizer = StepDict(Optimizer, **optimizer)

        else:
            raise TypeError(f"Unexpected type {type(optimizer)} for `optimizer`")

    def set_scheduler(self
                      ) -> None:
        """ Set scheduler(s) for optimizer(s). You can override as ::

            class YourTrainer(TrainerBase):
                def set_scheduler(self):
                    self.scheduler = torch.optim.lr_scheduler.Foo(self.optimizer)

        :return:
        """

        scheduler = self.scheduler
        if scheduler is not None and self.optimizer is None:
            raise TypeError("Optimizer is not set, so scheduler cannot be set")

        if isinstance(scheduler, Scheduler) or scheduler is None:
            self.scheduler = scheduler

        elif isinstance(scheduler, Partial):
            self.scheduler = scheduler(self.optimizer)

        elif isinstance(scheduler, dict):
            if not isinstance(self.optimizer, StepDict):
                raise TypeError("When `scheduler` is `dict`, `optimizer` is also needs to be `dict`")

            _scheduler = {}
            for k, v in scheduler.items():
                if isinstance(v, Partial):
                    v = v(self.optimizer[k])
                _scheduler[k] = v
            self.scheduler = StepDict(Scheduler, **_scheduler)

        else:
            raise TypeError(f"Unexpected type {type(scheduler)} for `scheduler`")


class SupervisedTrainer(TrainerBase):
    """ A simple trainer for supervised image classification. It only accepts single model. AMP-ready.

    """

    def __init__(self,
                 model: nn.Module,
                 optimizer: Optimizer,
                 loss_f: Callable,
                 *,
                 reporters: Optional[List[_ReporterBase]] = None,
                 scheduler: Optional[Scheduler] = None,
                 verb=True,
                 use_cudnn_benchmark=True,
                 data_parallel=False,
                 use_amp=False,
                 report_accuracy_topk: Optional[int] = None,
                 **kwargs):
        if isinstance(model, dict):
            raise TypeError(f"{type(self)} does not support dict model")
        super(SupervisedTrainer, self).__init__(model, optimizer, loss_f, reporters=reporters, scheduler=scheduler,
                                                verb=verb, use_cudnn_benchmark=use_cudnn_benchmark, **kwargs)

        if data_parallel and not isinstance(self.model, nn.DataParallel) and torch.cuda.device_count() > 1:
            self.model = nn.DataParallel(self.model)
            self.model.to(self.device)

        self._use_amp = use_amp
        if self._use_amp:
            self.scaler = torch.cuda.amp.GradScaler()
            self.logger.info("AMP is activated")
        self._report_topk = report_accuracy_topk

    def iteration(self,
                  data: Tuple[Tensor, Tensor]
                  ) -> None:
        input, target = data
        with torch.cuda.amp.autocast(self._use_amp):
            output = self.model(input)
            loss = self.loss_f(output, target)

        if self.is_train:
            self.optimizer.zero_grad()
            if self._use_amp:
                self.scaler.scale(loss).backward()
                self.scaler.step(self.optimizer)
                self.scaler.update()
            else:
                loss.backward()
                self.optimizer.step()

        self.reporter.add('accuracy', accuracy(output, target))
        self.reporter.add('loss', loss.detach_())
        if self._report_topk is not None:
            self.reporter.add(f'accuracy@{self._report_topk}', accuracy(output, target, self._report_topk))

    def state_dict(self
                   ) -> Mapping[str, Any]:

        return {'model': self.accessible_model.state_dict(),
                'optim': self.optimizer.state_dict(),
                'scheduler': self.scheduler.state_dict(),
                'epoch': self.epoch,
                'update_scheduler_by_epoch': self._update_scheduler_by_epoch,
                'use_sync_bn': self._use_sync_bn,
                'use_amp': self._use_amp}

    def load_state_dict(self,
                        state_dict: Mapping[str, Any]
                        ) -> None:
        self.accessible_model.load_state_dict(state_dict['model'])
        self.optimizer.load_state_dict(state_dict['optim'])
        self.scheduler.load_state_dict(state_dict['scheduler'])
        self.scheduler.last_epoch = state_dict['epoch']
        self._epoch = state_dict['epoch']
        self._update_scheduler_by_epoch = state_dict['update_scheduler_by_epoch']
        self._use_sync_bn = state_dict['use_sync_bn']
        self._use_amp = state_dict['use_amp']
