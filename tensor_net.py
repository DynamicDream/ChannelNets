import sys
import argparse
import numpy as np
import os

import tensorflow as tf

from tensorpack import logger, QueueInput
from tensorpack.models import *
from tensorpack.callbacks import *
from tensorpack.train import TrainConfig, SyncMultiGPUTrainerParameterServer
from tensorpack.dataflow import imgaug, FakeData
from tensorpack.tfutils import argscope, get_model_loader
from tensorpack.utils.gpu import get_nr_gpu

from imagenet_utils import (
    fbresnet_augmentor, get_imagenet_dataflow, ImageNetModel,
    eval_on_ILSVRC12)


class Model(ImageNetModel):
    def __init__(self, model, data_format='NCHW'):
        super(Model, self).__init__(data_format)
        self.model = model

    def get_logits(self, image):
        with argscope([Conv2D, MaxPooling, GlobalAvgPooling, BatchNorm], data_format=self.data_format):
            return self.model.inference(image)


def get_data(datadir, name, batch):
    isTrain = name == 'train'
    augmentors = fbresnet_augmentor(isTrain)
    return get_imagenet_dataflow(datadir, name, batch, augmentors)


def get_config(model, fake=False):
    nr_tower = max(get_nr_gpu(), 1)
    batch = model.conf.batch

    if fake:
        logger.info("For benchmark, batch size is fixed to 64 per tower.")
        dataset_train = FakeData(
            [[64, 224, 224, 3], [64]], 1000, random=False, dtype='uint8')
        callbacks = []
    else:
        logger.info("Running on {} towers. Batch size per tower: {}".format(nr_tower, batch))
        dataset_train = get_data('train', batch)
        dataset_val = get_data('val', batch)
        callbacks = [
            ModelSaver(),
            ScheduledHyperParamSetter('learning_rate',
                                      [(30, 1e-2), (60, 1e-3), (85, 1e-4), (95, 1e-5), (105, 1e-6)]),
            HumanHyperParamSetter('learning_rate'),
        ]
        infs = [ClassificationError('wrong-top1', 'val-error-top1'),
                ClassificationError('wrong-top5', 'val-error-top5')]
        if nr_tower == 1:
            # single-GPU inference with queue prefetch
            callbacks.append(InferenceRunner(QueueInput(dataset_val), infs))
        else:
            # multi-GPU inference (with mandatory queue prefetch)
            callbacks.append(DataParallelInferenceRunner(
                dataset_val, infs, list(range(nr_tower))))

    return TrainConfig(
        model=model,
        dataflow=dataset_train,
        callbacks=callbacks,
        steps_per_epoch=5000,
        max_epoch=110,
        nr_tower=nr_tower
    )


def run(model):
    instance = Model(model, model.conf.data_format)
    if !model.conf.is_train:
        batch = 64
        dataset = get_data(model.conf.data_dir, 'val', batch)
        eval_on_ILSVRC12(instance, get_model_loader(model.conf.test_step), dataset)
    else:
        logger.set_logger_dir(
            os.path.join('train_log', 'imagenet-resnet-d' + str(args.depth)))
        config = get_config(instance, fake=model.conf.fake)
        if model.conf.reload_step:
            config.session_init = get_model_loader(model.conf.reload_step)
        SyncMultiGPUTrainerParameterServer(config).train()