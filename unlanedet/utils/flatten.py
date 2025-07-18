import collections
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
import torch
from torch import nn

from contextlib import ExitStack, contextmanager
from unittest import mock

import fvcore
from fvcore.nn import activation_count, flop_count, parameter_count, parameter_count_table

@dataclass
class Schema:
    """
    A Schema defines how to flatten a possibly hierarchical object into tuple of
    primitive objects, so it can be used as inputs/outputs of PyTorch's tracing.

    PyTorch does not support tracing a function that produces rich output
    structures (e.g. dict, Instances, Boxes). To trace such a function, we
    flatten the rich object into tuple of tensors, and return this tuple of tensors
    instead. Meanwhile, we also need to know how to "rebuild" the original object
    from the flattened results, so we can evaluate the flattened results.
    A Schema defines how to flatten an object, and while flattening it, it records
    necessary schemas so that the object can be rebuilt using the flattened outputs.

    The flattened object and the schema object is returned by ``.flatten`` classmethod.
    Then the original object can be rebuilt with the ``__call__`` method of schema.

    A Schema is a dataclass that can be serialized easily.
    """

    # inspired by FetchMapper in tensorflow/python/client/session.py

    @classmethod
    def flatten(cls, obj):
        raise NotImplementedError

    def __call__(self, values):
        raise NotImplementedError

    @staticmethod
    def _concat(values):
        ret = ()
        sizes = []
        for v in values:
            assert isinstance(v, tuple), "Flattened results must be a tuple"
            ret = ret + v
            sizes.append(len(v))
        return ret, sizes

    @staticmethod
    def _split(values, sizes):
        if len(sizes):
            expected_len = sum(sizes)
            assert (
                len(values) == expected_len
            ), f"Values has length {len(values)} but expect length {expected_len}."
        ret = []
        for k in range(len(sizes)):
            begin, end = sum(sizes[:k]), sum(sizes[: k + 1])
            ret.append(values[begin:end])
        return ret
    
@dataclass
class ListSchema(Schema):
    schemas: List[Schema]  # the schemas that define how to flatten each element in the list
    sizes: List[int]  # the flattened length of each element

    def __call__(self, values):
        values = self._split(values, self.sizes)
        if len(values) != len(self.schemas):
            raise ValueError(
                f"Values has length {len(values)} but schemas " f"has length {len(self.schemas)}!"
            )
        values = [m(v) for m, v in zip(self.schemas, values)]
        return list(values)

    @classmethod
    def flatten(cls, obj):
        res = [flatten_to_tuple(k) for k in obj]
        values, sizes = cls._concat([k[0] for k in res])
        return values, cls([k[1] for k in res], sizes)


@dataclass
class TupleSchema(ListSchema):
    def __call__(self, values):
        return tuple(super().__call__(values))


@dataclass
class IdentitySchema(Schema):
    def __call__(self, values):
        return values[0]

    @classmethod
    def flatten(cls, obj):
        return (obj,), cls()


@dataclass
class DictSchema(ListSchema):
    keys: List[str]

    def __call__(self, values):
        values = super().__call__(values)
        return dict(zip(self.keys, values))

    @classmethod
    def flatten(cls, obj):
        for k in obj.keys():
            if not isinstance(k, str):
                raise KeyError("Only support flattening dictionaries if keys are str.")
        keys = sorted(obj.keys())
        values = [obj[k] for k in keys]
        ret, schema = ListSchema.flatten(values)
        return ret, cls(schema.schemas, schema.sizes, keys)

# if more custom structures needed in the future, can allow
# passing in extra schemas for custom types
def flatten_to_tuple(obj):
    """
    Flatten an object so it can be used for PyTorch tracing.
    Also returns how to rebuild the original object from the flattened outputs.

    Returns:
        res (tuple): the flattened results that can be used as tracing outputs
        schema: an object with a ``__call__`` method such that ``schema(res) == obj``.
             It is a pure dataclass that can be serialized.
    """
    schemas = [
        ((str, bytes), IdentitySchema),
        (list, ListSchema),
        (tuple, TupleSchema),
        (collections.abc.Mapping, DictSchema),
    ]
    for klass, schema in schemas:
        if isinstance(obj, klass):
            F = schema
            break
    else:
        F = IdentitySchema

    return F.flatten(obj)

@contextmanager
def patch_builtin_len(modules=()):
    """
    Patch the builtin len() function of a few detectron2 modules
    to use __len__ instead, because __len__ does not convert values to
    integers and therefore is friendly to tracing.

    Args:
        modules (list[stsr]): names of extra modules to patch len(), in
            addition to those in detectron2.
    """

    def _new_len(obj):
        return obj.__len__()

    with ExitStack() as stack:
        MODULES = list(modules)
        ctxs = [stack.enter_context(mock.patch(mod + ".len")) for mod in MODULES]
        for m in ctxs:
            m.side_effect = _new_len
        yield



class TracingAdapter(nn.Module):
    """
    A model may take rich input/output format (e.g. dict or custom classes),
    but `torch.jit.trace` requires tuple of tensors as input/output.
    This adapter flattens input/output format of a model so it becomes traceable.

    It also records the necessary schema to rebuild model's inputs/outputs from flattened
    inputs/outputs.

    Example:
    ::
        outputs = model(inputs)   # inputs/outputs may be rich structure
        adapter = TracingAdapter(model, inputs)

        # can now trace the model, with adapter.flattened_inputs, or another
        # tuple of tensors with the same length and meaning
        traced = torch.jit.trace(adapter, adapter.flattened_inputs)

        # traced model can only produce flattened outputs (tuple of tensors)
        flattened_outputs = traced(*adapter.flattened_inputs)
        # adapter knows the schema to convert it back (new_outputs == outputs)
        new_outputs = adapter.outputs_schema(flattened_outputs)
    """

    flattened_inputs: Tuple[torch.Tensor] = None
    """
    Flattened version of inputs given to this class's constructor.
    """

    inputs_schema: Schema = None
    """
    Schema of the inputs given to this class's constructor.
    """

    outputs_schema: Schema = None
    """
    Schema of the output produced by calling the given model with inputs.
    """

    def __init__(
        self,
        model: nn.Module,
        inputs,
        inference_func: Optional[Callable] = None,
        allow_non_tensor: bool = False,
    ):
        """
        Args:
            model: an nn.Module
            inputs: An input argument or a tuple of input arguments used to call model.
                After flattening, it has to only consist of tensors.
            inference_func: a callable that takes (model, *inputs), calls the
                model with inputs, and return outputs. By default it
                is ``lambda model, *inputs: model(*inputs)``. Can be override
                if you need to call the model differently.
            allow_non_tensor: allow inputs/outputs to contain non-tensor objects.
                This option will filter out non-tensor objects to make the
                model traceable, but ``inputs_schema``/``outputs_schema`` cannot be
                used anymore because inputs/outputs cannot be rebuilt from pure tensors.
                This is useful when you're only interested in the single trace of
                execution (e.g. for flop count), but not interested in
                generalizing the traced graph to new inputs.
        """
        super().__init__()
        if isinstance(model, (nn.parallel.distributed.DistributedDataParallel, nn.DataParallel)):
            model = model.module
        self.model = model
        if not isinstance(inputs, tuple):
            inputs = (inputs,)
        self.inputs = inputs
        self.allow_non_tensor = allow_non_tensor

        if inference_func is None:
            inference_func = lambda model, *inputs: model(*inputs)  # noqa
        self.inference_func = inference_func

        self.flattened_inputs, self.inputs_schema = flatten_to_tuple(inputs)

        if all(isinstance(x, torch.Tensor) for x in self.flattened_inputs):
            return
        if self.allow_non_tensor:
            self.flattened_inputs = tuple(
                [x for x in self.flattened_inputs if isinstance(x, torch.Tensor)]
            )
            self.inputs_schema = None
        else:
            for input in self.flattened_inputs:
                if not isinstance(input, torch.Tensor):
                    raise ValueError(
                        "Inputs for tracing must only contain tensors. "
                        f"Got a {type(input)} instead."
                    )

    def forward(self, *args: torch.Tensor):
        with torch.no_grad(), patch_builtin_len():
            if self.inputs_schema is not None:
                inputs_orig_format = self.inputs_schema(args)
            else:
                if len(args) != len(self.flattened_inputs) or any(
                    x is not y for x, y in zip(args, self.flattened_inputs)
                ):
                    raise ValueError(
                        "TracingAdapter does not contain valid inputs_schema."
                        " So it cannot generalize to other inputs and must be"
                        " traced with `.flattened_inputs`."
                    )
                inputs_orig_format = self.inputs

            outputs = self.inference_func(self.model, *inputs_orig_format)
            flattened_outputs, schema = flatten_to_tuple(outputs)

            flattened_output_tensors = tuple(
                [x for x in flattened_outputs if isinstance(x, torch.Tensor)]
            )
            if len(flattened_output_tensors) < len(flattened_outputs):
                if self.allow_non_tensor:
                    flattened_outputs = flattened_output_tensors
                    self.outputs_schema = None
                else:
                    raise ValueError(
                        "Model cannot be traced because some model outputs "
                        "cannot flatten to tensors."
                    )
            else:  # schema is valid
                if self.outputs_schema is None:
                    self.outputs_schema = schema
                else:
                    assert self.outputs_schema == schema, (
                        "Model should always return outputs with the same "
                        "structure so it can be traced!"
                    )
            return flattened_outputs

    def _create_wrapper(self, traced_model):
        """
        Return a function that has an input/output interface the same as the
        original model, but it calls the given traced model under the hood.
        """

        def forward(*args):
            flattened_inputs, _ = flatten_to_tuple(args)
            flattened_outputs = traced_model(*flattened_inputs)
            return self.outputs_schema(flattened_outputs)

        return forward
    
# Some extra ops to ignore from counting, including elementwise and reduction ops
_IGNORED_OPS = {
    "aten::add",
    "aten::add_",
    "aten::argmax",
    "aten::argsort",
    "aten::batch_norm",
    "aten::constant_pad_nd",
    "aten::div",
    "aten::div_",
    "aten::exp",
    "aten::log2",
    "aten::max_pool2d",
    "aten::meshgrid",
    "aten::mul",
    "aten::mul_",
    "aten::neg",
    "aten::nonzero_numpy",
    "aten::reciprocal",
    "aten::repeat_interleave",
    "aten::rsub",
    "aten::sigmoid",
    "aten::sigmoid_",
    "aten::softmax",
    "aten::sort",
    "aten::sqrt",
    "aten::sub",
    "torchvision::nms",  # TODO estimate flop for nms
}    
    
class FlopCountAnalysis(fvcore.nn.FlopCountAnalysis):
    """
    Same as :class:`fvcore.nn.FlopCountAnalysis`, but supports detectron2 models.
    """

    def __init__(self, model, inputs):
        """
        Args:
            model (nn.Module):
            inputs (Any): inputs of the given model. Does not have to be tuple of tensors.
        """
        wrapper = TracingAdapter(model, inputs, allow_non_tensor=True)
        super().__init__(wrapper, wrapper.flattened_inputs)
        self.set_op_handle(**{k: None for k in _IGNORED_OPS})
        
        


def flop_count_operators(model: nn.Module, inputs: list):
    """
    Implement operator-level flops counting using jit.
    This is a wrapper of :func:`fvcore.nn.flop_count` and adds supports for standard
    detection models in detectron2.
    Please use :class:`FlopCountAnalysis` for more advanced functionalities.

    Note:
        The function runs the input through the model to compute flops.
        The flops of a detection model is often input-dependent, for example,
        the flops of box & mask head depends on the number of proposals &
        the number of detected objects.
        Therefore, the flops counting using a single input may not accurately
        reflect the computation cost of a model. It's recommended to average
        across a number of inputs.

    Args:
        model: a detectron2 model that takes `list[dict]` as input.
        inputs (list[dict]): inputs to model, in detectron2's standard format.
            Only "image" key will be used.
        supported_ops (dict[str, Handle]): see documentation of :func:`fvcore.nn.flop_count`

    Returns:
        Counter: Gflop count per operator
    """
    old_train = model.training
    model.eval()
    ret = FlopCountAnalysis(model, inputs).by_operator()
    model.train(old_train)
    return {k: v / 1e9 for k, v in ret.items()}

FLOPS_MODE = "flops"
ACTIVATIONS_MODE = "activations"

def activation_count_operators(
    model: nn.Module, inputs: list, **kwargs
):
    """
    Implement operator-level activations counting using jit.
    This is a wrapper of fvcore.nn.activation_count, that supports standard detection models
    in detectron2.

    Note:
        The function runs the input through the model to compute activations.
        The activations of a detection model is often input-dependent, for example,
        the activations of box & mask head depends on the number of proposals &
        the number of detected objects.

    Args:
        model: a detectron2 model that takes `list[dict]` as input.
        inputs (list[dict]): inputs to model, in detectron2's standard format.
            Only "image" key will be used.

    Returns:
        Counter: activation count per operator
    """
    return _wrapper_count_operators(model=model, inputs=inputs, mode=ACTIVATIONS_MODE, **kwargs)


def _wrapper_count_operators(
    model: nn.Module, inputs: list, mode: str, **kwargs
):
    # ignore some ops
    supported_ops = {k: lambda *args, **kwargs: {} for k in _IGNORED_OPS}
    supported_ops.update(kwargs.pop("supported_ops", {}))
    kwargs["supported_ops"] = supported_ops

    assert len(inputs) == 1, "Please use batch size=1"
    tensor_input = inputs[0]["image"]
    inputs = [{"image": tensor_input}]  # remove other keys, in case there are any

    old_train = model.training
    if isinstance(model, (nn.parallel.distributed.DistributedDataParallel, nn.DataParallel)):
        model = model.module
    wrapper = TracingAdapter(model, inputs)
    wrapper.eval()
    if mode == FLOPS_MODE:
        ret = flop_count(wrapper, (tensor_input,), **kwargs)
    elif mode == ACTIVATIONS_MODE:
        ret = activation_count(wrapper, (tensor_input,), **kwargs)
    else:
        raise NotImplementedError("Count for mode {} is not supported yet.".format(mode))
    # compatible with change in fvcore
    if isinstance(ret, tuple):
        ret = ret[0]
    model.train(old_train)
    return ret


def find_unused_parameters(model: nn.Module, inputs) -> List[str]:
    """
    Given a model, find parameters that do not contribute
    to the loss.

    Args:
        model: a model in training mode that returns losses
        inputs: argument or a tuple of arguments. Inputs of the model

    Returns:
        list[str]: the name of unused parameters
    """
    assert model.training
    for _, prm in model.named_parameters():
        prm.grad = None

    if isinstance(inputs, tuple):
        losses = model(*inputs)
    else:
        losses = model(inputs)

    if isinstance(losses, dict):
        losses = sum(losses.values())
    losses.backward()

    unused: List[str] = []
    for name, prm in model.named_parameters():
        if prm.grad is None:
            unused.append(name)
        prm.grad = None
    return unused