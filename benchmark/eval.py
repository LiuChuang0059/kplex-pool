import sys
import argparse
import numpy as np
from math import ceil
from itertools import product

import torch

import torch_geometric
from torch_geometric.datasets import TUDataset

import skorch
from skorch import NeuralNetClassifier
from skorch.dataset import CVSplit, Dataset
from skorch.helper import predefined_split

from benchmark import model
from kplex_pool.utils import add_node_features
from kplex_pool.kplex import KPlexCover
from kplex_pool.data import NDPDataset, CustomDataset

from sklearn.model_selection import StratifiedShuffleSplit

from .add_pool import add_pool, add_pool_x



torch_geometric.nn.add_pool = add_pool
torch_geometric.nn.add_pool_x = add_pool_x


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default='CoverPool')
    parser.add_argument('--dataset', type=str, default='PROTEINS')
    parser.add_argument('--cover_priority', type=str, default='default')
    parser.add_argument('--kplex_priority', type=str, default='default')
    parser.add_argument('--global_pool_op', type=str, nargs='+', default=['add', 'max'])
    parser.add_argument('--node_pool_op', type=str, nargs='+', default=['add'])
    parser.add_argument('--edge_pool_op', type=str, default='add')
    parser.add_argument('--jumping_knowledge', type=str, default='cat')
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--batch_size', type=int, default=-1)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--weight_decay', type=float, default=0.001)
    parser.add_argument('--dropout', type=float, default=0.3)
    parser.add_argument('--simplify', action='store_true')
    parser.add_argument('--dense', action='store_true')
    parser.add_argument('--easy', action='store_true')
    parser.add_argument('--small', action='store_true')
    parser.add_argument('--ratio', type=float, default=0.8)
    parser.add_argument('--split', type=float, default=0.1)
    parser.add_argument('--layers', type=int, default=3)
    parser.add_argument('--inner_layers', type=int, default=2)
    parser.add_argument('--hidden', type=int, default=64)
    parser.add_argument('--method', type=str, default='softmax')
    parser.add_argument('--edge_dropout', type=float, default=0.2)
    parser.add_argument('--q', type=float, default=None)
    parser.add_argument('--k', type=int, default=8)
    parser.add_argument('--k_step_factor', type=float, default=0.5)
    parser.add_argument('--graph_sage', action='store_true')
    parser.add_argument('--normalize', action='store_true')
    parser.add_argument('--skip_covered', action='store_true')
    parser.add_argument('--no_readout', action='store_false')
    parser.add_argument('--no_cache', action='store_false')
    parser.add_argument('--ks', nargs='*', type=int)
    args = parser.parse_args()

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    torch.manual_seed(42)
    np.random.seed(42)

    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(42)

    if args.dataset == 'NDPDataset':
        train, val, test = (NDPDataset('data/', 
                                       split=key, 
                                       easy=args.easy, 
                                       small=args.small) 
                            for key in ['train', 'val', 'test'])
        
        train_size = len(train) + len(val)
        dataset = CustomDataset(list(train) + list(val) + list(test))

        X = np.arange(len(dataset)).reshape((-1, 1))
        y = dataset.data.y.numpy()

        cv_split = predefined_split(Dataset(X[train_size:], y[train_size:]))
        X, y = X[:train_size], y[:train_size]
    else:
        dataset = TUDataset(root='data/' + args.dataset, name=args.dataset)

        if dataset.data.x is None:
            dataset = add_node_features(dataset)

        X = np.arange(len(dataset)).reshape((-1, 1))
        y = dataset.data.y.numpy()

        cv_split = CVSplit(cv=StratifiedShuffleSplit(test_size=args.split, n_splits=1, random_state=42))

    params = {
        'module': getattr(model, args.model), 
        'module__dataset': dataset,
        'module__num_layers': args.layers,
        'module__hidden': args.hidden,
        'module__graph_sage': args.graph_sage,
        'module__dropout': args.dropout,
        'module__num_inner_layers': args.inner_layers,
        'module__jumping_knowledge': args.jumping_knowledge,
        'module__normalize': args.normalize,
        'module__readout': args.no_readout,
        'module__global_pool_op': args.global_pool_op,
        'module__device':device,
        'max_epochs': args.epochs,
        'batch_size': args.batch_size,
        'lr': args.lr,
        'criterion': model.PoolLoss if args.model == 'DiffPool' else torch.nn.modules.loss.NLLLoss,
        'optimizer': torch.optim.Adam,
        'optimizer__weight_decay': args.weight_decay,
        'iterator_train__shuffle': True,
        'train_split': cv_split,
        'device': device
    }

    if args.model == 'CoverPool':
        ks = args.ks

        if ks is None:
            ks = [args.k]
            last_k = float(args.k)

            for _ in range(2, args.layers):
                last_k *= args.k_step_factor
                ks.append(ceil(last_k))

        kplex_cover = KPlexCover(args.cover_priority, args.kplex_priority, args.skip_covered)
        cover_fun = kplex_cover.get_cover_fun(ks, dataset if args.no_cache else None, 
                                              dense=args.dense,
                                              q=args.q,
                                              simplify=args.simplify,
                                              edge_pool_op=args.edge_pool_op,
                                              verbose=True if args.no_cache else False)
        params.update(
            module__cover_fun=cover_fun,
            module__node_pool_op=args.node_pool_op,
            module__dense=args.dense
        )
    elif args.model == 'EdgePool':
        params.update(module__method=args.method, module__edge_dropout=args.edge_dropout)
    elif args.model == 'BaseModel':
        params.update(module__dense=args.dense)
    elif args.model == 'Graclus':
        params.update(module__node_pool_op=args.node_pool_op)
    else:
        params.update(module__ratio=args.ratio)

    NeuralNetClassifier(**params).fit(X, y)
