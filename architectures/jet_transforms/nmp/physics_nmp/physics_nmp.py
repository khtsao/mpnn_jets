import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from data_ops.batching import batch_leaves

from architectures.readout import construct_readout
from architectures.embedding import construct_embedding
from ..stacked_nmp.attention_pooling import construct_pooling_layer
from .adjacency import construct_physics_based_adjacency_matrix
from ..message_passing import construct_mp_layer

class PhysicsNMP(nn.Module):
    def __init__(self,
        features=None,
        hidden=None,
        iters=None,
        readout=None,
        **kwargs
        ):
        super().__init__()
        self.iters = iters
        self.embedding = construct_embedding('simple', features + 1, hidden, act=kwargs.get('act', None))
        self.mp_layers = nn.ModuleList([construct_mp_layer('physics', hidden=hidden,**kwargs) for _ in range(iters)])
        self.readout = construct_readout(readout, hidden, hidden)
        self.alpha = kwargs.pop('alpha', None)
        self.R = kwargs.pop('R', None)
        self.trainable_physics = kwargs.pop('trainable_physics', None)

    @property
    def adjacency_matrix(self):
        return construct_physics_based_adjacency_matrix(
                        alpha=self.alpha,
                        R=self.R,
                        trainable_physics=self.trainable_physics
                        )

    def forward(self, jets, mask=None, **kwargs):
        h = self.embedding(jets)
        dij = self.adjacency_matrix(jets)
        #import ipdb; ipdb.set_trace()
        for mp in self.mp_layers:
            h, A = mp(h=h, mask=mask, dij=dij, **kwargs)
        out = self.readout(h)
        return out, A

class OnesNMP(PhysicsNMP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def adjacency_matrix(self):
        def ones(jets):
            bs, sz, _ = jets.size()
            matrix = Variable(torch.ones(bs, sz, sz))
            if torch.cuda.is_available():
                matrix = matrix.cuda()
            return matrix
        return ones

class PhysicsStackNMP(nn.Module):
    def __init__(self,
        features=None,
        hidden=None,
        iters=None,
        readout=None,
        scales=None,
        mp_layer=None,
        pooling_layer=None,
        **kwargs
        ):
        super().__init__()
        self.iters = iters
        self.embedding = construct_embedding('simple', features + 1, hidden, act=kwargs.get('act', None))
        self.physics_nmp = PhysicsNMP(features, hidden, 1, readout='constant', **kwargs)
        self.readout = construct_readout(readout, hidden, hidden)
        self.attn_pools = nn.ModuleList([construct_pooling_layer(pooling_layer, scales[i], hidden) for i in range(len(scales))])
        self.nmps = nn.ModuleList([construct_mp_layer(mp_layer, hidden=hidden, **kwargs) for _ in range(len(scales))])

    def forward(self, jets, mask=None, **kwargs):
        h, _ = self.physics_nmp(jets, mask, **kwargs)
        for pool, nmp in zip(self.attn_pools, self.nmps):
            h = pool(h)
            h, A = nmp(h=h, mask=None, **kwargs)
        out = self.readout(h)
        return out, A
