from ..modelzoo import get_config

import os
from omegaconf import OmegaConf
from unlanedet.config import LazyCall as L

from unlanedet.model.GSENet.detector import GSENet
from unlanedet.model.GSENet.head import GSEHead
from unlanedet.model import ResNetWrapper,FPN

# import dataset and transform
from unlanedet.data.transform import *

from fvcore.common.param_scheduler import CosineParamScheduler

iou_loss_weight = 2.
cls_loss_weight = 6.
xyt_loss_weight = 0.5
seg_loss_weight = 1.0
num_points = 72
max_lanes = 5
sample_y = range(710, 150, -10)
img_norm = dict(mean=[103.939, 116.779, 123.68], std=[1., 1., 1.])
ori_img_w = 1280
ori_img_h = 720
img_h = 320
img_w = 800
cut_height = 160 
num_classes = 6 + 1
ignore_label = 255
bg_weight = 0.4
featuremap_out_channel = 192
test_parameters = dict(conf_threshold=0.4, nms_thres=50, nms_topk=max_lanes)
data_root = "/home/dataset/tusimple"

param_config = OmegaConf.create()
param_config.iou_loss_weight = iou_loss_weight
param_config.cls_loss_weight = cls_loss_weight
param_config.xyt_loss_weight = xyt_loss_weight
param_config.seg_loss_weight = seg_loss_weight
param_config.num_points = num_points
param_config.max_lanes = max_lanes
param_config.sample_y = [i for i in range(710, 150, -10)]
param_config.test_parameters = test_parameters
param_config.ori_img_w = ori_img_w
param_config.ori_img_h = ori_img_h
param_config.img_w = img_w
param_config.img_h = img_h
param_config.cut_height = cut_height
param_config.img_norm = img_norm
param_config.data_root = data_root
param_config.ignore_label = ignore_label
param_config.bg_weight = bg_weight
param_config.featuremap_out_channel = featuremap_out_channel
param_config.num_classes = num_classes

model = L(GSENet)(
    backbone = L(ResNetWrapper)(
        resnet='resnet18',
        pretrained=True,
        replace_stride_with_dilation=[False, False, False],
        out_conv=False,        
    ),
    neck = L(FPN)(
        in_channels=[128, 256, 512],
        out_channels=64,
        num_outs=3,
        attention=False),
    head = L(GSEHead)(
        num_priors=192,
        refine_layers=3,
        fc_hidden_dim=64,
        sample_points=36,
        cfg=param_config
    )
)


train = get_config("config/common/train.py").train
epochs =70
batch_size = 32
epoch_per_iter = (3616 // batch_size + 1)
total_iter = epoch_per_iter * epochs 
train.max_iter = total_iter
train.checkpointer.period=epoch_per_iter
train.eval_period = epoch_per_iter

optimizer = get_config("config/common/optim.py").AdamW
optimizer.lr = 0.8e-3
optimizer.weight_decay = 0.01


lr_multiplier = L(CosineParamScheduler)(
    start_value = 1,
    end_value = 0.001
)

train_process = [
    L(GenerateLaneLine)(
        transforms = [
            dict(name='Resize',
                 parameters=dict(size=dict(height=img_h, width=img_w)),
                 p=1.0),
            dict(name='HorizontalFlip', parameters=dict(p=1.0), p=0.5),
            dict(name='ChannelShuffle', parameters=dict(p=1.0), p=0.1),
            dict(name='MultiplyAndAddToBrightness',
                 parameters=dict(mul=(0.85, 1.15), add=(-10, 10)),
                 p=0.6),
            dict(name='AddToHueAndSaturation',
                 parameters=dict(value=(-10, 10)),
                 p=0.7),
            dict(name='OneOf',
                 transforms=[
                     dict(name='MotionBlur', parameters=dict(k=(3, 5))),
                     dict(name='MedianBlur', parameters=dict(k=(3, 5)))
                 ],
                 p=0.2),
            dict(name='Affine',
                 parameters=dict(translate_percent=dict(x=(-0.1, 0.1),
                                                        y=(-0.1, 0.1)),
                                 rotate=(-10, 10),
                                 scale=(0.8, 1.2)),
                 p=0.7),
            dict(name='Resize',
                 parameters=dict(size=dict(height=img_h, width=img_w)),
                 p=1.0),            
        ],
        cfg = param_config
    ),
    L(ToTensor)(keys=['img', 'lane_line', 'seg']),
]

val_process = [
    L(GenerateLaneLine)(
         transforms=[
             dict(name='Resize',
                  parameters=dict(size=dict(height=img_h, width=img_w)),
                  p=1.0),
         ],
         training=False,
         cfg = param_config        
    ),
    L(ToTensor)(keys=['img'])
]

dataloader = get_config("config/common/tusimple.py").dataloader
dataloader.train.dataset.processes = train_process
dataloader.train.dataset.data_root = data_root
dataloader.train.dataset.cut_height = cut_height
dataloader.train.total_batch_size = batch_size
dataloader.test.dataset.processes = val_process
dataloader.test.dataset.data_root = data_root
dataloader.test.dataset.cut_height = cut_height
dataloader.test.total_batch_size = batch_size

# Evaluation config
dataloader.evaluator.output_basedir = "./output"
dataloader.evaluator.test_json_file=os.path.join(data_root,"test_label.json")










