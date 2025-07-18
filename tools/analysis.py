import logging
import numpy as np
from collections import Counter
import tqdm
from fvcore.nn import flop_count_table  # can also try flop_count_str'

from unlanedet.checkpoint import Checkpointer
from unlanedet.config import LazyConfig, instantiate
from unlanedet.engine import default_argument_parser

from unlanedet.utils.flatten import (
    FlopCountAnalysis,
    activation_count_operators,
    parameter_count_table,
)

from unlanedet.utils.logger import setup_logger

logger = logging.getLogger("unlanedet")

def setup(args):
    cfg = LazyConfig.load(args.config_file)
    cfg = LazyConfig.apply_overrides(cfg, args.opts)
    setup_logger(name="fvcore")
    setup_logger()
    return cfg

def do_flop(cfg):
    data_loader = instantiate(cfg.dataloader.test)
    model = instantiate(cfg.model)
    model.to(cfg.train.device)
    Checkpointer(model).load(cfg.train.init_checkpoint)
    model.eval()

    counts = Counter()
    total_flops = []
    for idx, data in zip(tqdm.trange(args.num_inputs), data_loader):  # noqa
        flops = FlopCountAnalysis(model, data)
        if idx > 0:
            flops.unsupported_ops_warnings(False).uncalled_modules_warnings(False)
        counts += flops.by_operator()
        total_flops.append(flops.total())

    logger.info("Flops table computed from only one input sample:\n" + flop_count_table(flops))
    logger.info(
        "Average GFlops for each type of operators:\n"
        + str([(k, v / (idx + 1) / 1e9) for k, v in counts.items()])
    )
    logger.info(
        "Total GFlops: {:.1f}±{:.1f}".format(np.mean(total_flops) / 1e9, np.std(total_flops) / 1e9)
    )


def do_activation(cfg):
    data_loader = instantiate(cfg.dataloader.test)
    model = instantiate(cfg.model)
    model.to(cfg.train.device)
    Checkpointer(model).load(cfg.train.init_checkpoint)
    model.eval()

    counts = Counter()
    total_activations = []
    for idx, data in zip(tqdm.trange(args.num_inputs), data_loader):  # noqa
        count = activation_count_operators(model, data)
        counts += count
        total_activations.append(sum(count.values()))
    logger.info(
        "(Million) Activations for Each Type of Operators:\n"
        + str([(k, v / idx) for k, v in counts.items()])
    )
    logger.info(
        "Total (Million) Activations: {}±{}".format(
            np.mean(total_activations), np.std(total_activations)
        )
    )


def do_parameter(cfg):
    model = instantiate(cfg.model)
    logger.info("Parameter Count:\n" + parameter_count_table(model, max_depth=5))


def do_structure(cfg):
    model = instantiate(cfg.model)
    logger.info("Model Structure:\n" + str(model))


if __name__ == "__main__":
    parser = default_argument_parser(
        epilog="""
Examples:
To show parameters of a model:
$ python tools/analyze_model.py --tasks parameter \\
    --config-file projects/dab_detr/configs/dab_detr_r50_50ep.py
Flops and activations are data-dependent, therefore inputs and model weights
are needed to count them:
$ python tools/analyze_model.py --num-inputs 100 --tasks flop \\
    --config-file projects/dab_detr/configs/dab_detr_r50_50ep.py \\
    train.init_checkpoint=/path/to/model.pkl
"""
    )
    parser.add_argument('--ckpt', help='The path of checkpoint')
    parser.add_argument(
        "--tasks",
        choices=["flop", "activation", "parameter", "structure"],
        required=True,
        nargs="+",
    )
    parser.add_argument(
        "-n",
        "--num-inputs",
        default=100,
        type=int,
        help="number of inputs used to compute statistics for flops/activations, "
        "both are data dependent.",
    )
    args = parser.parse_args()
    assert not args.eval_only
    assert args.num_gpus == 1

    cfg = setup(args)
    
    cfg.train.init_checkpoint = args.ckpt

    for task in args.tasks:
        {
            "flop": do_flop,
            "activation": do_activation,
            "parameter": do_parameter,
            "structure": do_structure,
        }[task](cfg)