import json
import warnings
from logging import Logger
from pathlib import Path
from typing import Dict, Type, Tuple, Callable, List
import uuid

import cpuinfo
import numpy as np
import torch


from nebullvm.base import ModelCompiler, DeepLearningFramework, ModelParams
from nebullvm.config import NEBULLVM_DEBUG_FILE
from nebullvm.inference_learners.base import BaseInferenceLearner
from nebullvm.measure import compute_optimized_running_time
from nebullvm.optimizers import (
    BaseOptimizer,
    TensorRTOptimizer,
    ApacheTVMOptimizer,
    OpenVinoOptimizer,
    ONNXOptimizer,
)

COMPILER_TO_OPTIMIZER_MAP: Dict[ModelCompiler, Type[BaseOptimizer]] = {
    ModelCompiler.APACHE_TVM: ApacheTVMOptimizer,
    ModelCompiler.OPENVINO: OpenVinoOptimizer,
    ModelCompiler.TENSOR_RT: TensorRTOptimizer,
    ModelCompiler.ONNX_RUNTIME: ONNXOptimizer,
}


def _tvm_is_available() -> bool:
    try:
        import tvm  # noqa F401

        return True
    except ImportError:
        return False


def select_compilers_from_hardware():
    compilers = [ModelCompiler.ONNX_RUNTIME]
    if _tvm_is_available():
        compilers.append(ModelCompiler.APACHE_TVM)
    if torch.cuda.is_available():
        compilers.append(ModelCompiler.TENSOR_RT)
    cpu_raw_info = cpuinfo.get_cpu_info()["brand_raw"].lower()
    if "intel" in cpu_raw_info:
        compilers.append(ModelCompiler.OPENVINO)
    return compilers


def _optimize_with_compiler(
    compiler: ModelCompiler,
    logger: Logger,
    metric_func: Callable = None,
    **kwargs,
) -> Tuple[BaseInferenceLearner, float]:
    optimizer = COMPILER_TO_OPTIMIZER_MAP[compiler](logger)
    return _optimize_with_optimizer(optimizer, logger, metric_func, **kwargs)


def _save_info(optimizer: BaseOptimizer, score: float, debug_file: str):
    if Path(debug_file).exists():
        with open(debug_file, "r") as f:
            old_dict = json.load(f)
    else:
        old_dict = {}
    old_dict[optimizer.__class__.__name__] = f"{score}"
    with open(debug_file, "w") as f:
        json.dump(old_dict, f)


def _optimize_with_optimizer(
    optimizer: BaseOptimizer,
    logger: Logger,
    metric_func: Callable = None,
    debug_file: str = None,
    **kwargs,
) -> Tuple[BaseInferenceLearner, float]:
    if metric_func is None:
        metric_func = compute_optimized_running_time
    try:
        model_optimized = optimizer.optimize(**kwargs)
        latency = metric_func(model_optimized)
    except Exception as ex:
        warning_msg = (
            f"Compilation failed with {optimizer.__class__.__name__}. "
            f"Got error {ex}. The optimizer will be skipped."
        )
        if logger is None:
            warnings.warn(warning_msg)
        else:
            logger.warning(warning_msg)
        latency = np.inf
        model_optimized = None
    if debug_file:
        _save_info(optimizer, latency, debug_file)
    return model_optimized, latency


class MultiCompilerOptimizer(BaseOptimizer):
    """Run all the optimizers available for the given hardware and select the
    best optimized model in terms of either latency or user defined
    performance.

    Attributes:
        logger (Logger, optional): User defined logger.
        ignore_compilers (List[str], optional): List of compilers that must
            be ignored.
        extra_optimizers (List[BaseOptimizer], optional): List of optimizers
            defined by the user. It usually contains optimizers specific for
            user-defined tasks or optimizers built for the specific model.
            Note that, if given, the optimizers must be already initialized,
            i.e. they could have a different Logger than the one defined in
            `MultiCompilerOptimizer`.
        debug_mode (bool, optional): Boolean flag for activating the debug
            mode. When activated, all the performances of the the different
            containers  will be stored in a json file saved in the working
            directory. Default is False.
    """

    def __init__(
        self,
        logger: Logger = None,
        ignore_compilers: List = None,
        extra_optimizers: List[BaseOptimizer] = None,
        debug_mode: bool = False,
    ):
        super().__init__(logger)
        self.compilers = [
            compiler
            for compiler in select_compilers_from_hardware()
            if compiler not in (ignore_compilers or [])
        ]
        self.extra_optimizers = extra_optimizers
        self.debug_file = (
            f"{uuid.uuid4()}_{NEBULLVM_DEBUG_FILE}" if debug_mode else None
        )

    def optimize(
        self,
        onnx_model: str,
        output_library: DeepLearningFramework,
        model_params: ModelParams,
    ) -> BaseInferenceLearner:
        """Optimize the ONNX model using the available compilers.

        Args:
            onnx_model (str): Path to the ONNX model.
            output_library (DeepLearningFramework): Framework of the optimized
                model (either torch on tensorflow).
            model_params (ModelParams): Model parameters.

        Returns:
            BaseInferenceLearner: Model optimized for inference.
        """
        optimized_models = [
            _optimize_with_compiler(
                compiler,
                logger=self.logger,
                onnx_model=onnx_model,
                output_library=output_library,
                model_params=model_params,
                debug_file=self.debug_file,
            )
            for compiler in self.compilers
        ]
        if self.extra_optimizers is not None:
            optimized_models += [
                _optimize_with_optimizer(
                    op,
                    logger=self.logger,
                    onnx_model=onnx_model,
                    output_library=output_library,
                    model_params=model_params,
                    debug_file=self.debug_file,
                )
                for op in self.extra_optimizers
            ]
        optimized_models.sort(key=lambda x: x[1], reverse=False)
        return optimized_models[0][0]

    def optimize_on_custom_metric(
        self,
        metric_func: Callable,
        onnx_model: str,
        output_library: DeepLearningFramework,
        model_params: ModelParams,
        return_all: bool = False,
    ):
        """Optimize the ONNX model using the available compilers and give the
        best result sorting by user-defined metric.

        Args:
            metric_func (Callable): function which should be used for sorting
                the compiled models. The metric_func should take as input an
                InferenceLearner and return a numerical value. Note that the
                outputs will be sorted in an ascendant order, i.e. the compiled
                model with the smallest value will be selected.
            onnx_model (str): Path to the ONNX model.
            output_library (DeepLearningFramework): Framework of the optimized
                model (either torch on tensorflow).
            model_params (ModelParams): Model parameters.
            return_all (bool, optional): Boolean flag. If true the method
                returns the tuple (compiled_model, score) for each available
                compiler. Default `False`.

        Returns:
            Union[BaseInferenceLearner, Tuple[BaseInferenceLearner, float]]:
                The method returns just a model optimized for inference if
                `return_all` is `False` or all the compiled models and their
                scores otherwise.
        """
        optimized_models = [
            _optimize_with_compiler(
                compiler,
                metric_func=metric_func,
                logger=self.logger,
                onnx_model=onnx_model,
                output_library=output_library,
                model_params=model_params,
                debug_mode=self.debug_mode,
            )
            for compiler in self.compilers
        ]
        if self.extra_optimizers is not None:
            optimized_models += [
                _optimize_with_optimizer(
                    op,
                    logger=self.logger,
                    onnx_model=onnx_model,
                    output_library=output_library,
                    model_params=model_params,
                    debug_mode=self.debug_mode,
                )
                for op in self.extra_optimizers
            ]
        if return_all:
            return optimized_models
        optimized_models.sort(key=lambda x: x[1], reverse=False)
        return optimized_models[0][0]

    @property
    def usable(self) -> bool:
        return len(self.compilers) > 0 or (
            self.extra_optimizers is not None
            and len(self.extra_optimizers) > 0
        )
