from math import ceil
from tqdm import tqdm

import torch
import torch.nn.functional as F
from torch.nn import Linear, BatchNorm1d, ModuleList
from torch.utils.data.dataloader import default_collate

import torch_geometric
from torch_geometric.transforms import ToDense
from torch_geometric.data import Batch, Data, Dataset
from torch_geometric.nn import (
    GCNConv, 
    SAGEConv, 
    DenseGCNConv, 
    DenseSAGEConv, 
    JumpingKnowledge, 
    TopKPooling,
    SAGPooling,
    EdgePooling,
    dense_diff_pool, 
    global_max_pool, 
    global_add_pool, 
    global_mean_pool
)

from kplex_pool import KPlexCover, cover_pool_node, cover_pool_edge, simplify
from kplex_pool.utils import hub_promotion
from kplex_pool.data import CustomDataset, DenseDataset



class Block(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, num_layers=2, 
                 mode='cat', graph_sage=False, dense=False):
        super(Block, self).__init__()

        if dense:
            module = DenseSAGEConv if graph_sage else DenseGCNConv
        else:
            module = SAGEConv if graph_sage else GCNConv

        self.mode = mode
        self.dense = dense
        self.graph_sage = graph_sage
        self.convs = ModuleList([
            module(in_channels if l == 0 else hidden_channels, 
                   hidden_channels if mode or l < num_layers - 1 else out_channels) 
            for l in range(num_layers)
        ])

        if mode:
            self.jump = JumpingKnowledge(mode, hidden_channels, num_layers)

            if mode == 'cat':
                self.lin = Linear(hidden_channels*num_layers, out_channels)
            else:
                self.lin = Linear(hidden_channels, out_channels)

    def reset_parameters(self):
        for conv in self.convs:
            conv.reset_parameters()
        
        if self.mode:
            self.lin.reset_parameters()
            self.jump.reset_parameters()

    def forward(self, data):
        x = data.x
        xs = []

        for conv in self.convs:
            if self.dense:
                x = conv(x, data.adj, mask=data.mask, add_loop=True)
            else:
                if self.graph_sage:
                    x = conv(x, data.edge_index)
                else:
                    x = conv(x, data.edge_index, data.edge_attr)
            
            x = F.relu(x)
            xs.append(x)

        if self.mode:
            x = F.relu(self.lin(self.jump(xs)))

            if self.dense:
                x = x * data.mask.unsqueeze(-1).type(x.dtype)
        
        return x


class BaseModel(torch.nn.Module):
    def __init__(self, dataset, hidden,
                 num_layers=3, 
                 num_inner_layers=2, 
                 dropout=0.3,
                 readout=True, 
                 graph_sage=False,
                 dense=False, 
                 normalize=False, 
                 jumping_knowledge='cat',
                 global_pool_op='add',
                 device=None):
        super(BaseModel, self).__init__()

        if device is None:
            self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self.device = device

        self.hidden = hidden
        self.num_layers = num_layers
        self.num_inner_layers = num_inner_layers
        self.jumping_knowledge = jumping_knowledge
        self.normalize = normalize
        self.graph_sage = graph_sage
        self.dense = dense
        self.readout = readout
        self.dataset = DenseDataset(dataset) if dense else dataset
        self.dropout = dropout

        gps = global_pool_op if isinstance(global_pool_op, list) else [global_pool_op]
        
        if dense:
            self.global_pool_op = []

            for op in gps:
                if callable(op):
                    self.global_pool_op.append(op)
                elif op == 'add':
                    self.global_pool_op.append(lambda x, _1, _2: torch.sum(x, dim=1))
                elif op == 'max' or op == 'min':
                    self.global_pool_op.append(lambda x, _1, _2: getattr(torch, op)(x, dim=1)[0])
                else:
                    self.global_pool_op.append(lambda x, _1, _2: getattr(torch, op)(x, dim=1))
        else:
            self.global_pool_op = [getattr(torch_geometric.nn, f'global_{op}_pool') for op in gps]

        self.conv_in = Block(dataset.num_features, hidden, hidden, num_inner_layers, jumping_knowledge, graph_sage, dense)
        self.blocks = torch.nn.ModuleList()
        
        for _ in range(num_layers - 1):
            self.blocks.append(Block(hidden, hidden, hidden, num_inner_layers, jumping_knowledge, graph_sage, dense))

        out_dim = len(gps) * num_layers * hidden if readout else len(gps) * hidden
        self.bn = BatchNorm1d(out_dim)
        self.lin1 = Linear(out_dim, hidden)
        self.lin2 = Linear(hidden, dataset.num_classes)
    
    def collate(self, index):
        if self.dense:
            return self.dataset[index.view(-1)].to(self.device)
        
        return Batch.from_data_list(self.dataset[index.view(-1)]).to(self.device)

    def reset_parameters(self):
        self.conv_in.reset_parameters()

        for block in self.blocks:
            block.reset_parameters()

        self.bn.reset_parameters()            
        self.lin1.reset_parameters()
        self.lin2.reset_parameters()
    
    def global_pool(self, x, batch, batch_size):
        return [op(x, batch, batch_size) for op in self.global_pool_op]

    def pool(self, data, layer):
        return data

    def forward(self, index):
        data = self.collate(index)
        batch_size = len(index)

        data.x = self.conv_in(data)
        xs = self.global_pool(data.x, data.batch, batch_size)

        for layer, block in enumerate(self.blocks):  
            data = self.pool(data, layer)

            if self.normalize:
                data.x = F.normalize(data.x)

            data.x = block(data)
            xs.extend(self.global_pool(data.x, data.batch, batch_size))
        
        if not self.readout:
            xs = xs[-2:]
        
        x = torch.cat(xs, dim=1)
        x = self.bn(x)
        x = F.relu(self.lin1(x))
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.lin2(x)

        return F.softmax(x, dim=-1)

    def __repr__(self):
        return self.__class__.__name__


class CoverPool(BaseModel):
    def __init__(self, cover_fun, node_pool_op='add', **kwargs):
        super(CoverPool, self).__init__(**kwargs)

        self.cover_fun = cover_fun
        self.hierarchy = []
        self.node_pool_op = node_pool_op if isinstance(node_pool_op, list) else [node_pool_op]

        if len(self.node_pool_op) > 1:
            self.blocks = torch.nn.ModuleList()
            
            for _ in range(self.num_layers - 1):
                self.blocks.append(Block(len(self.node_pool_op)*self.hidden, self.hidden, self.hidden, 
                                         self.num_inner_layers, self.jumping_knowledge, self.graph_sage, self.dense))

    def collate(self, index):
        self.hierarchy = self.cover_fun(self.dataset, index.view(-1).to(self.device))
        
        if self.dense:
            self.hierarchy = [data.to(self.device) for data in self.hierarchy]
        else:
            self.hierarchy = [Batch.from_data_list(data).to(self.device) for data in self.hierarchy]

        return self.hierarchy[0]

    def pool(self, data, layer):
        cover = self.hierarchy[layer + 1]
        xs = []

        for op in self.node_pool_op:
            xs.append(cover_pool_node(data.cover_index, data.x, cover.num_nodes, op, self.dense, data.cover_mask if self.dense else None))
        
        cover.x = torch.cat(xs, dim=-1)

        return cover

# Other models used for comparison, slightly modified from
# https://github.com/rusty1s/pytorch_geometric/tree/master/benchmark/kernel

class DiffPool(BaseModel):
    def __init__(self, ratio=0.25, **kwargs):
        super(DiffPool, self).__init__(dense=True, **kwargs)

        num_nodes = self.dataset.max_nodes
        self.link_loss, self.ent_loss = 0., 0.

        if isinstance(ratio, list):
            self.num_layers = len(ratio) + 1
            self.ratios = ratio
        else:
            self.ratios = [ratio for _ in range(1, self.num_layers)]

        self.pool_blocks = torch.nn.ModuleList()

        for layer, ratio in enumerate(self.ratios):
            num_nodes = ceil(ratio * float(num_nodes))
            self.pool_blocks.append(Block(self.dataset.num_features if layer == 0 else self.hidden, self.hidden, num_nodes, 
                                          self.num_inner_layers, self.jumping_knowledge, self.graph_sage, True))

    def reset_parameters(self):
        super(DiffPool, self).reset_parameters()

        for block in self.pool_blocks:
            block.reset_parameters()
    
    def collate(self, index):
        data = super(DiffPool, self).collate(index)
        data.old_x = data.x

        return data
    
    def pool(self, data, layer):
        data.x, data.old_x = data.old_x, data.x
        s = self.pool_blocks[layer](data)
        data.x, data.adj, link_loss, ent_loss = dense_diff_pool(data.old_x, data.adj, s, data.mask)
        data.old_x = data.x
        data.mask = torch.ones(data.x.size()[:-1], dtype=torch.uint8)

        if layer == 0:
            self.link_loss = link_loss
            self.ent_loss = ent_loss
        else:
            self.link_loss += link_loss
            self.ent_loss += ent_loss

        return data

    def forward(self, index):
        return super(DiffPool, self).forward(index), self.link_loss, self.ent_loss


class PoolLoss(torch.nn.Module):
    def __init__(self, link_weight=1., ent_weight=1., *args, **kwargs):
        super(PoolLoss, self).__init__()
        self.loss = torch.nn.modules.loss.NLLLoss(*args, **kwargs)
        self.link_weight = link_weight
        self.ent_weight = ent_weight

    def forward(self, input, target):
        output, link_loss, ent_loss = input
        output = torch.log(output)

        return self.loss.forward(output, target) \
            + self.link_weight*link_loss \
            + self.ent_weight*ent_loss


class TopKPool(BaseModel):
    def __init__(self, ratio=0.8, **kwargs):
        super(TopKPool, self).__init__(dense=False, **kwargs)
        self.ratio = ratio

        if isinstance(ratio, list):
            self.num_layers = len(ratio) + 1
            self.ratios = ratio
        else:
            self.ratios = [ratio for _ in range(1, self.num_layers)]

        self.pool_blocks = torch.nn.ModuleList([
            TopKPooling(self.hidden, r) for r in self.ratios
        ])

    def reset_parameters(self):
        super(TopKPool, self).reset_parameters()

        for pool in self.pool_blocks:
            pool.reset_parameters()
    
    def pool(self, data, layer):
        data.x, data.edge_index, data.edge_attr, data.batch, _, _ = self.pool_blocks[layer](data.x, data.edge_index, 
                                                                                            data.edge_attr, data.batch)

        return data

class SAGPool(BaseModel):
    def __init__(self, ratio=0.5, gnn='GCNConv', **kwargs):
        super(SAGPool, self).__init__(dense=False, **kwargs)
        self.ratio = ratio
        self.gnn = getattr(torch_geometric.nn, gnn) if isinstance(gnn, str) else gnn

        if isinstance(ratio, list):
            self.num_layers = len(ratio) + 1
            self.ratios = ratio
        else:
            self.ratios = [ratio for _ in range(1, self.num_layers)]

        self.pool_blocks = torch.nn.ModuleList([
            SAGPooling(self.hidden, r, GNN=self.gnn) for r in self.ratios
        ])

    def reset_parameters(self):
        super(SAGPool, self).reset_parameters()

        for pool in self.pool_blocks:
            pool.reset_parameters()
    
    def pool(self, data, layer):
        data.x, data.edge_index, data.edge_attr, data.batch, _, _ = self.pool_blocks[layer](data.x, data.edge_index, 
                                                                                            data.edge_attr, data.batch)

        return data


class EdgePool(BaseModel):
    def __init__(self, method='softmax', edge_dropout=0., **kwargs):
        super(EdgePool, self).__init__(dense=False, **kwargs)
        self.method = getattr(EdgePooling, 'compute_edge_score_' + method) if isinstance(method, str) else method
        self.edge_dropout = edge_dropout

        self.pool_blocks = torch.nn.ModuleList([
            EdgePooling(self.hidden, self.method, edge_dropout, 0.) for i in range(1, self.num_layers)
        ])

    def reset_parameters(self):
        super(EdgePool, self).reset_parameters()

        for pool in self.pool_blocks:
            pool.reset_parameters()
    
    def pool(self, data, layer):
        data.x, data.edge_index, data.batch, _ = self.pool_blocks[layer](data.x, data.edge_index, data.batch)

        return data
