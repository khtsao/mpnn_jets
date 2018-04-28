import os
import logging
from collections import OrderedDict

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter

from src.architectures.nmp.message_passing.vertex_update import GRUUpdate

from src.admin.utils import memory_snapshot
from ..ProteinModel import ProteinModel
from ...loss import loss as loss_fn

def conv_and_pad(in_planes, out_planes, kernel_size=17, stride=1):
    # "3x3 convolution with padding"
    padding = (kernel_size - 1) // 2
    return nn.Conv1d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, bias=False)

class BasicBlock(nn.Module):
    expansion = 1
    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        m = OrderedDict()
        m['conv1'] = conv_and_pad(inplanes, planes, stride)
        m['bn1'] = nn.BatchNorm1d(planes)
        m['relu1'] = nn.ReLU(inplace=True)
        m['conv2'] = conv_and_pad(planes, planes)
        m['bn2'] = nn.BatchNorm1d(planes)
        self.group1 = nn.Sequential(m)

        self.relu= nn.Sequential(nn.ReLU(inplace=True))
        self.downsample = downsample

    def forward(self, x):

        if self.downsample is not None:
            residual = self.downsample(x)
        else:
            residual = x

        out = self.group1(x) + residual

        out = self.relu(out)

        del residual

        return out

def squared_distance_matrix(x, y):
    '''
    Calculate the pairwise squared distances between two batches of matrices x and y.
    Input
        x: a tensor of shape (bs, n, d)
        y: a tensor of shape (bs, m, d)
    Output
        dist: a tensor of shape (bs, n, m) where dist[i,j,k] = || x[i,j] - y[i,k] || ^ 2
    '''
    bs = x.size(0)
    assert bs == y.size(0)

    n = x.size(1)
    m = y.size(1)

    d = x.size(2)
    assert d == y.size(2)

    x = x.unsqueeze(2).expand(bs, n, m, d)
    y = y.unsqueeze(1).expand(bs, n, m, d)

    dist = torch.pow(x - y, 2).sum(3)

    return dist

class ConvolutionalNMPBlock(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.spatial_embedding = nn.Linear(dim, 3)

        self.conv1d = BasicBlock(dim, dim)

        self.message = nn.Sequential(
                        nn.Linear(dim, dim),
                        nn.ReLU(inplace=True)
                        )

        self.update = GRUUpdate(2 * dim, dim)

    def forward(self, x, batch_mask, y, y_mask):
        x_conv = self.conv1d(x.transpose(1,2)).transpose(1,2)

        s = self.spatial_embedding(x)
        A = torch.exp( - squared_distance_matrix(s, s) ) * batch_mask
        x_nmp = torch.bmm(A, self.message(x))

        x_in = torch.cat([x_conv, x_nmp], -1)

        x = self.update(x, x_in)

        loss = loss_fn(A, y, y_mask, batch_mask)

        return x, loss

class BasicNMPBlock(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.spatial_embedding = nn.Linear(dim, 3)

        self.message = nn.Sequential(
                        nn.Linear(dim, dim),
                        nn.ReLU(inplace=True)
                        )

        self.update = GRUUpdate(dim, dim)

    def forward(self, x, batch_mask, y, y_mask):
        s = self.spatial_embedding(x)
        A = torch.exp( - squared_distance_matrix(s, s) ) * batch_mask
        x_nmp = torch.bmm(A, self.message(x))
        x = self.update(x, x_nmp)

        loss = loss_fn(A, y, y_mask, batch_mask)

        return x, loss

class ConvolutionOnlyBlock(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv1d = BasicBlock(dim, dim)

    def forward(self, x, *args, **kwargs):
        x = self.conv1d(x.transpose(1,2)).transpose(1,2)
        return x, 0.

class GraphGen(ProteinModel):
    def __init__(self,
        features=None,
        hidden=None,
        iters=None,
        no_grad=False,
        tied=False,
        block=None,
        polar=False,
        **kwargs
        ):

        super().__init__()

        self.no_grad = no_grad
        self.polar = polar
        self.initial_embedding = nn.Linear(features, hidden)


        if block == 'cnmp':
            NMPBlock = ConvolutionalNMPBlock
        elif block == 'nmp':
            NMPBlock = BasicNMPBlock
        elif block == 'conv':
            NMPBlock = ConvolutionOnlyBlock
        else:
            raise ValueError

        self.final_spatial_embedding = nn.Linear(hidden, 3)

        if tied:
            nmp_block = NMPBlock(hidden)
            nmp_block.spatial_embedding = self.final_spatial_embedding
            self.nmp_blocks = nn.ModuleList([nmp_block] * iters)
        else:
            self.nmp_blocks = nn.ModuleList([NMPBlock(hidden) for _ in range(iters)])

        #self.scale = nn.Parameter(torch.zeros(1))
        #self.scale = torch.zeros(1)

    def forward(self, x, mask, y, y_mask, **kwargs):
        s, l = self.get_final_spatial_embedding(x, mask, y, y_mask, **kwargs)
        A = torch.exp( - squared_distance_matrix(s,s)) * mask
        return A, l

    def get_final_spatial_embedding(self, x, mask, y, y_mask, **kwargs):
        if self.no_grad:
            return self.get_final_spatial_embedding_no_grad(x, mask, y, y_mask, **kwargs)
        return self.get_final_spatial_embedding_with_grad(x, mask, y, y_mask, **kwargs)

    def get_final_spatial_embedding_no_grad(self, x, mask, y, y_mask, **kwargs):
        n_volatile_layers = np.random.randint(0, len(self.nmp_blocks))

        if n_volatile_layers > 0:
            x = Variable(x.data, volatile=True)
            x = self.initial_embedding(x)
            for i in range(n_volatile_layers):
                nmp = self.nmp_blocks[i]
                x = nmp(x, mask)
            x = Variable(x.data)
            x = self.nmp_blocks[n_volatile_layers](x, mask)
        else:
            x = self.initial_embedding(x)

        s = self.final_spatial_embedding(x)
        return s, None


    def get_final_spatial_embedding_with_grad(self, x, mask, y, y_mask, **kwargs):
        l = 0
        x = self.initial_embedding(x)
        for nmp in self.nmp_blocks:
            x, l_ = nmp(x, mask, y=y, y_mask=y_mask)
            l = l + l_
        s = self.final_spatial_embedding(x)
        return s, l

    def loss_and_pred(self, x, batch_mask, y, y_mask, **kwargs):
        A, l = self(x, batch_mask, y, y_mask)
        return l + self.loss_fn(A, y, y_mask, batch_mask), A
