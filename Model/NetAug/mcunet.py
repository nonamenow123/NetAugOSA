from typing import List

import torch.nn as nn

from Model.base.layers import OpSequential, ResidualBlock
from Model.base.mcunet import MCUNet
from Model.NetAug.layers import (
    DynamicConvLayer,
    DynamicDsConvLayer,
    DynamicInvertedBlock,
    DynamicLinearLayer,
)
from Model.NetAug.utils import aug_width, sync_width
from util.misc import make_divisible, torch_random_choices

from Model.NetAug.mbv2 import NetAugMobileNetV2

__all__ = ["NetAugMCUNet"]


class NetAugMCUNet(NetAugMobileNetV2):
    def __init__(
        self,
        base_net: MCUNet,
        aug_expand_list: List[float],
        aug_width_mult_list: List[float],
        n_classes: int,
        dropout_rate=0.0,
    ):
        nn.Module.__init__(self)
        max_width_mult = max(aug_width_mult_list)

        # input stem
        base_input_stem = base_net.backbone["input_stem"]
        aug_input_stem = OpSequential(
            [
                DynamicConvLayer(
                    3,
                    aug_width(
                        base_input_stem.op_list[0].out_channels, aug_width_mult_list, 1
                    ),
                    stride=2,
                    act_func="relu6",
                ),
                ResidualBlock(
                    DynamicDsConvLayer(
                        make_divisible(
                            base_input_stem.op_list[0].out_channels * max_width_mult, 1
                        ),
                        aug_width(
                            base_input_stem.op_list[1].conv.out_channels,
                            aug_width_mult_list,
                            1,
                        ),
                        act_func=("relu6", None),
                    ),
                    shortcut=None,
                ),
            ]
        )

        # stages
        aug_stages = []
        for base_stage in base_net.backbone["stages"]:
            stage = []
            for base_block in base_stage.op_list:
                stage.append(
                    ResidualBlock(
                        DynamicInvertedBlock(
                            in_channels=make_divisible(
                                base_block.conv.in_channels * max_width_mult, 1
                            ),
                            out_channels=aug_width(
                                base_block.conv.out_channels, aug_width_mult_list, 1
                            ),
                            kernel_size=base_block.conv.kernel_size,
                            expand_ratio=aug_width(
                                base_block.conv.expand_ratio, aug_expand_list
                            ),
                            stride=base_block.conv.stride,
                            act_func=(
                                base_block.conv.inverted_conv.act,
                                base_block.conv.depth_conv.act,
                                base_block.conv.point_conv.act,
                            ),
                        ),
                        shortcut=base_block.shortcut,
                    )
                )
            aug_stages.append(OpSequential(stage))

        # head
        base_head = base_net.head
        aug_head = OpSequential(
            [
                ResidualBlock(
                    DynamicInvertedBlock(
                        make_divisible(
                            base_head.op_list[0].conv.in_channels * max_width_mult, 1
                        ),
                        aug_width(
                            base_head.op_list[0].conv.out_channels,
                            aug_width_mult_list,
                            1,
                        ),
                        base_head.op_list[0].conv.kernel_size,
                        expand_ratio=aug_width(
                            base_head.op_list[0].conv.expand_ratio, aug_expand_list
                        ),
                        act_func=("relu6", "relu6", None),
                    ),
                    shortcut=None,
                ),
                nn.AdaptiveAvgPool2d(1),
                DynamicLinearLayer(
                    make_divisible(
                        base_head.op_list[-1].in_features * max_width_mult, 1
                    ),
                    n_classes,
                    dropout_rate=dropout_rate,
                ),
            ]
        )

        self.backbone = nn.ModuleDict(
            {
                "input_stem": aug_input_stem,
                "stages": nn.ModuleList(aug_stages),
            }
        )
        self.head = aug_head

    def set_active(self, mode: str, sync=False, generator=None):
        # input stem
        first_conv, first_block = self.backbone["input_stem"].op_list
        if mode in ["min", "min_w"]:
            first_conv.conv.active_out_channels = min(first_conv.out_channels_list)
            first_block.conv.point_conv.conv.active_out_channels = min(
                first_block.conv.point_conv.out_channels_list
            )
        elif mode in ["random", "min_e"]:
            first_conv.conv.active_out_channels = torch_random_choices(
                first_conv.out_channels_list,
                generator,
            )
            first_block.conv.point_conv.conv.active_out_channels = torch_random_choices(
                first_block.conv.point_conv.out_channels_list,
                generator,
            )
        elif mode in ["max"]:
            first_conv.conv.active_out_channels = max(
                first_conv.out_channels_list
            )
            first_block.conv.point_conv.conv.active_out_channels = max(
                first_block.conv.point_conv.out_channels_list
            )
        else:
            raise NotImplementedError
        if sync:
            first_conv.conv.active_out_channels = sync_width(
                first_conv.conv.active_out_channels
            )
            first_block.conv.point_conv.conv.active_out_channels = sync_width(
                first_block.conv.point_conv.conv.active_out_channels
            )

        # stages
        in_channels = first_block.conv.point_conv.conv.active_out_channels
        for block in self.all_blocks:
            if block.shortcut is None:
                if mode in ["min", "min_w"]:
                    active_out_channels = min(block.conv.point_conv.out_channels_list)
                elif mode in ["random", "min_e"]:
                    active_out_channels = torch_random_choices(
                        block.conv.point_conv.out_channels_list,
                        generator,
                    )
                elif mode in ["max"]:
                    active_out_channels = max(
                        block.conv.point_conv.out_channels_list
                    )
                else:
                    raise NotImplementedError
            else:
                active_out_channels = in_channels
            if mode in ["min", "min_e"]:
                active_expand_ratio = min(block.conv.expand_ratio_list)
            elif mode in ["min_w", "random"]:
                active_expand_ratio = torch_random_choices(
                    block.conv.expand_ratio_list,
                    generator,
                )
            elif mode in ["max"]:
                active_expand_ratio = max(
                    block.conv.expand_ratio_list
                )
            else:
                raise NotImplementedError
            active_mid_channels = make_divisible(active_expand_ratio * in_channels, 1)
            if sync:
                active_mid_channels = sync_width(active_mid_channels)
                active_out_channels = sync_width(active_out_channels)

            block.conv.inverted_conv.conv.active_out_channels = active_mid_channels
            block.conv.point_conv.conv.active_out_channels = active_out_channels

            in_channels = active_out_channels

    def export(self) -> MCUNet:
        export_model = MCUNet.__new__(MCUNet)
        nn.Module.__init__(export_model)
        # input stem
        input_stem = OpSequential(
            [
                self.backbone["input_stem"].op_list[0].export(),
                ResidualBlock(
                    self.backbone["input_stem"].op_list[1].conv.export(),
                    self.backbone["input_stem"].op_list[1].shortcut,
                ),
            ]
        )

        # stages
        stages = []
        for stage in self.backbone["stages"]:
            blocks = []
            for block in stage.op_list:
                blocks.append(
                    ResidualBlock(
                        block.conv.export(),
                        block.shortcut,
                    )
                )
            stages.append(OpSequential(blocks))

        # head
        head = OpSequential(
            [
                ResidualBlock(
                    self.head.op_list[0].conv.export(),
                    self.head.op_list[0].shortcut,
                ),
                self.head.op_list[1],
                self.head.op_list[2].export(),
            ]
        )
        export_model.backbone = nn.ModuleDict(
            {
                "input_stem": input_stem,
                "stages": nn.ModuleList(stages),
            }
        )
        export_model.head = head
        return export_model

import torch

if __name__ == '__main__':
    print('start')
    basemodel = MCUNet(num_classes=4, width=1)
    model = NetAugMCUNet(base_net=basemodel, aug_width_mult_list=[1, 2], aug_expand_list=[1], n_classes=4)
    model.sort_channels()