import torch

from kplex_pool import kplex_cpu
from kplex_pool.pool import cover_pool_node, cover_pool_edge
from kplex_pool.simplify import simplify as simplify_graph
from kplex_pool.utils import hub_promotion
from kplex_pool.data import Cover, CustomDataset

from tqdm import tqdm



class KPlexCover:
    def __init__(self, cover_priority="default", kplex_priority="default", skip_covered=False):
        if cover_priority == "default":
            cover_priority = ["min_degree", "min_uncovered"]
    
        if kplex_priority == "default":
            kplex_priority = ["max_in_kplex", "max_candidates", "min_uncovered"]
    
        if not isinstance(cover_priority, list):
            cover_priority = [cover_priority]
    
        if not isinstance(kplex_priority, list):
            kplex_priority = [kplex_priority]
            
        self.cover_priority = []
        self.kplex_priority = []
        self.skip_covered = skip_covered
    
        for p in cover_priority:
            cp = getattr(kplex_cpu.NodePriority, p, None)
    
            if cp is None or cp in {
                        kplex_cpu.NodePriority.max_in_kplex,
                        kplex_cpu.NodePriority.min_in_kplex,
                        kplex_cpu.NodePriority.max_candidates,
                        kplex_cpu.NodePriority.min_candidates
                    }:
                raise ValueError('Not a valid priority: %s' % p)
            
            self.cover_priority.append(cp)
            
        for p in kplex_priority:
            kp = getattr(kplex_cpu.NodePriority, p, None)
    
            if kp is None:
                raise ValueError('Not a valid priority: %s' % p)
            
            self.kplex_priority.append(kp)
    
    def __call__(self, k, edge_index, num_nodes=None, batch=None):
        device = edge_index.device

        if num_nodes is None:
            num_nodes = edge_index.max().item() + 1

        if batch is None:
            row, col = edge_index.cpu()
            cover_index = kplex_cpu.kplex_cover(row, col, k, int(num_nodes),
                                                self.cover_priority,
                                                self.kplex_priority,
                                                self.skip_covered).to(device)
            clusters = cover_index[1].max().item() + 1

            return cover_index, clusters, cover_index.new_zeros(clusters)

        count = batch.bincount(minlength=batch[-1] + 1)
        out_index = []
        out_batch = []
        out_clusters = 0
        min_index = 0

        for b, num_nodes in enumerate(count):
            mask = batch[edge_index[0]] == b
            cover_index, clusters, zeros = self(k, edge_index[:, mask] - min_index, num_nodes)
            cover_index[0].add_(min_index)
            cover_index[1].add_(out_clusters)

            out_index.append(cover_index)
            out_batch.append(zeros.add_(b))
            out_clusters += clusters
            min_index += num_nodes

        return torch.cat(out_index, dim=1), out_clusters, torch.cat(out_batch, dim=0)

    def process(self, dataset, k, 
                edge_pool_op='add', 
                q=None, 
                simplify=False, 
                verbose=True):        
        it = tqdm(dataset, desc="Processing dataset", leave=False) if verbose else dataset
        in_list = []
        out_list = []
        
        for data in it:
            cover_index, clusters, _ = self(k, data.edge_index, data.num_nodes)
            
            if q is not None:
                cover_index, clusters, _ = hub_promotion(cover_index, q=q, 
                                                         num_nodes=data.num_nodes, 
                                                         num_clusters=clusters)

            edge_index, weights = cover_pool_edge(cover_index, data.edge_index, data.edge_attr, 
                                                  data.num_nodes, clusters, pool=edge_pool_op)

            if simplify:
                edge_index, weights = simplify_graph(edge_index, weights, num_nodes=clusters)
            
            keys = dict(data.__iter__())
            keys['num_nodes'] = data.num_nodes
            in_list.append(Cover(cover_index=cover_index, num_clusters=clusters, **keys))
            out_list.append(Cover(edge_index=edge_index, edge_attr=weights, num_nodes=clusters))
        
        return CustomDataset(in_list), CustomDataset(out_list)

    def get_representations(self, dataset, ks, *args, **kwargs):
        last_dataset = dataset
        output = []

        if (len(args) >= 4 and args[3]) or kwargs.get('verbose', True):
            ks = tqdm(ks, desc="Creating Hierarchical Representations", leave=False)

        for k in ks:
            cover, last_dataset = self.process(last_dataset, k, *args, **kwargs)
            output.append(cover)

        output.append(last_dataset)
        
        return output

    def get_cover_fun(self, ks, dataset=None, *args, **kwargs):
        if dataset is None:
            return lambda ds, idx: self.get_representations(ds[idx], ks, *args, **kwargs)

        cache = self.get_representations(dataset, ks, *args, **kwargs)

        return lambda _, idx: [ds[idx] for ds in cache]
