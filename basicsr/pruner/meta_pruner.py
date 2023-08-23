import torch
import torch.nn as nn
import copy
import time
import numpy as np
from math import ceil, sqrt
from collections import OrderedDict
from basicsr.utils_ASSL import strdict_to_dict
from fnmatch import fnmatch, fnmatchcase
from basicsr.layer import LayerStruct
from .utils import get_pr_model,get_N2M_granularity, get_constrained_layers, pick_pruned_model, get_kept_filter_channel, replace_module, get_next_bn
from .utils import get_masks

class MetaPruner:
    def __init__(self, model, args, logger, passer):
        self.model = model
        self.args = args
        self.logger = logger
        self.logprint = self.logger.info if logger else print
        # self.netprint = logger.log_printer.netprint if logger else print

        # set up layers
        self.LEARNABLES = (nn.Conv2d, nn.Linear) # the layers we focus on for pruning
        layer_struct = LayerStruct(model, self.LEARNABLES)
        self.layers = layer_struct.layers
        self._max_len_ix = layer_struct._max_len_ix
        self._max_len_name = layer_struct._max_len_name
        self.layer_print_prefix = layer_struct.print_prefix

        # set up pr for each layer
        self.stage_pr = args.stage_pr
        self.stage_pr_lslen = args.stage_pr_lslen
        self.stage_pr_lsval = args.stage_pr_lsval
        if self.stage_pr_lslen and self.stage_pr_lsval:
            self.stage_pr = [self.stage_pr_lsval]*self.stage_pr_lslen
        self.raw_pr = get_pr_model(self.layers, self.stage_pr, skip=args.skip_layers, compare_mode=args.compare_mode)
        self.pr = copy.deepcopy(self.raw_pr)

        self.N2M_granularity = get_N2M_granularity(model, self.layers, self.args.pick_pruned)

        # pick pruned and kept weight groups
        self.constrained_layers = get_constrained_layers(self.layers, self.args.same_pruned_wg_layers)
        print(f'Constrained layers: {self.constrained_layers}')

    def _get_kept_wg_L1(self, model, align_constrained=False):
        # ************************* core pruning function **************************
        self.pr, global_pr, self.pruned_wg, self.kept_wg = pick_pruned_model(model, self.layers, self.raw_pr,
                                                        wg=self.args.wg, 
                                                        criterion=self.args.prune_criterion,
                                                        compare_mode=self.args.compare_mode,
                                                        sort_mode=self.args.pick_pruned,
                                                        constrained=self.constrained_layers,
                                                        align_constrained=align_constrained,
                                                        N2M_granularity=self.N2M_granularity)
        # ***************************************************************************
        
        # print
        self.logprint(f'*********** Get pruned wg ***********')
        for name, layer in self.layers.items():
            logtmp = f'{self.layer_print_prefix[name]} -- Got pruned wg by L1 sorting ({self.args.pick_pruned}), pr {self.pr[name]}, numel {len(layer.score)}'
            ext = f' -- This is a constrained layer. Its pruned/kept indices have been adjusted.' if name in self.constrained_layers else ''
            self.logprint(logtmp + ext)
        print(f'*************************************')

        # @WJM: note that N:M sparsity may lead to different pr from what raw N:M indicated
        self.logprint('Global pr upon the whole model statistic: %f'%global_pr)

    def _prune_and_build_new_model(self):
        if self.args.wg == 'weight':
            self.masks = get_masks(self.layers, self.pruned_wg)
            return

        new_model = copy.deepcopy(self.model)
        for name, m in self.model.named_modules():
            if isinstance(m, self.LEARNABLES):
                kept_filter, kept_chl = get_kept_filter_channel(self.layers, name, pr=self.pr, kept_wg=self.kept_wg, wg=self.args.wg)
                
                # decide if renit the current layer
                reinit = False
                for rl in self.args.reinit_layers:
                    if fnmatch(name, rl):
                        reinit = True
                        break
                
                # get number of channels (can be manually assigned)
                num_chl = self.args.layer_chl[name] if name in self.args.layer_chl else len(kept_chl)

                # copy weight and bias
                bias = False if isinstance(m.bias, type(None)) else True
                if isinstance(m, nn.Conv2d):
                    new_layer = nn.Conv2d(num_chl, len(kept_filter), m.kernel_size,
                                    m.stride, m.padding, m.dilation, m.groups, bias).cuda()
                    if not reinit:
                        kept_weights = m.weight.data[kept_filter][:, kept_chl, :, :]

                elif isinstance(m, nn.Linear):
                    kept_weights = m.weight.data[kept_filter][:, kept_chl]
                    new_layer = nn.Linear(in_features=len(kept_chl), out_features=len(kept_filter), bias=bias).cuda()
                
                if not reinit:
                    new_layer.weight.data.copy_(kept_weights) # load weights into the new module
                    if bias:
                        kept_bias = m.bias.data[kept_filter]
                        new_layer.bias.data.copy_(kept_bias)
                
                # load the new conv
                replace_module(new_model, name, new_layer)

                # get the corresponding bn (if any) for later use
                next_bn = get_next_bn(self.model, m)

            elif isinstance(m, nn.BatchNorm2d) and m == next_bn:
                new_bn = nn.BatchNorm2d(len(kept_filter), eps=m.eps, momentum=m.momentum, 
                        affine=m.affine, track_running_stats=m.track_running_stats).cuda()
                
                # copy bn weight and bias
                if self.args.copy_bn_w:
                    weight = m.weight.data[kept_filter]
                    new_bn.weight.data.copy_(weight)
                if self.args.copy_bn_b:
                    bias = m.bias.data[kept_filter]
                    new_bn.bias.data.copy_(bias)
                
                # copy bn running stats
                new_bn.running_mean.data.copy_(m.running_mean[kept_filter])
                new_bn.running_var.data.copy_(m.running_var[kept_filter])
                new_bn.num_batches_tracked.data.copy_(m.num_batches_tracked)
                
                # load the new bn
                replace_module(new_model, name, new_bn)
        self.model = new_model

        # print the layer shape of pruned model
        LayerStruct(new_model, self.LEARNABLES)
        return new_model