import os, os.path as osp
from time import strftime
import tqdm
import torch
from torch_geometric.data import DataLoader
import argparse

from torch_cmspepr.dataset import TauDataset
from torch_cmspepr.gravnet_model import GravnetModel
import torch_cmspepr.objectcondensation as oc
from lrscheduler import CyclicLRWithRestarts

torch.manual_seed(1009)


def softclip(array, start_clip_value):
    array /= start_clip_value
    array = torch.where(array>1, torch.log(array+1.), array)
    return array * start_clip_value

def calc_L_energy(pred_energy, truth_energy):
    diff = torch.abs(pred_energy - truth_energy)
    L = 10. * torch.exp(-0.1 * diff**2 ) + 0.01*diff
    return softclip(L, 10.)

def calc_L_time(pred_time, truth_time):
    return softclip(oc.huber(torch.abs(pred_time-truth_time), 2.), 6.)

def calc_L_position(pred_position: torch.Tensor, truth_position: torch.Tensor):
    d_squared = ((pred_position-truth_position)**2).sum(dim=-1)
    return softclip(oc.huber(torch.sqrt(d_squared/100. + 1e-2), 10.), 3.)

def calc_L_classification(pred_pid, truth_pid):
    raise NotImplementedError

def calc_Lp(
    pred_beta: torch.Tensor, truth_cluster_index,
    pred_cluster_properties, truth_cluster_properties,
    batch_size,
    return_components = False
    ):
    """
    Property loss

    For now, assumes:
    0 : energy,
    1,2 : boundary crossing position,

    Todo: time, pdgid
    """
    xi = torch.zeros_like(pred_beta)
    is_sig = truth_cluster_index > 0
    xi[is_sig] = pred_beta[is_sig].arctanh()

    L_energy = calc_L_energy(pred_cluster_properties[:,0], truth_cluster_properties[:,0])
    L_position = calc_L_position(pred_cluster_properties[:,1:3], truth_cluster_properties[:,1:3])
    # L_time = calc_L_time(pred_cluster_properties[:,1], truth_cluster_properties[:,1])
    # L_classification = calc_L_classification(pred_cluster_properties[:,4], pred_cluster_properties[:,4]) TODO

    xi_sum = xi.sum()
    def xi_weighting(L):
        return 1./xi_sum * (xi * L).sum() / batch_size

    L_p_energy_weighted = xi_weighting(L_energy)
    L_p_position_weighted = xi_weighting(L_position)
    L_p = L_p_energy_weighted + L_p_position_weighted
    
    if return_components:
        return dict(
            L_p_energy = L_p_energy_weighted,
            L_p_position = L_p_position_weighted,
            L_p = L_p,
            )
    else:
        return L_p


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--dry', action='store_true', help='Turn off checkpoint saving and run limited number of events')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print more output')
    parser.add_argument('--ckptdir', type=str)
    args = parser.parse_args()
    if args.verbose: oc.DEBUG = True

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print('Using device', device)

    n_epochs = 400
    batch_size = 4

    shuffle = True
    dataset = TauDataset('data/taus')
    dataset.blacklist([ # Remove a bunch of bad events
        'data/taus/110_nanoML_98.npz',
        'data/taus/113_nanoML_13.npz',
        'data/taus/124_nanoML_77.npz',
        'data/taus/128_nanoML_70.npz',
        'data/taus/149_nanoML_90.npz',
        'data/taus/153_nanoML_22.npz',
        'data/taus/26_nanoML_93.npz',
        'data/taus/32_nanoML_45.npz',
        'data/taus/5_nanoML_51.npz',
        'data/taus/86_nanoML_97.npz',
        ])
    if args.dry:
        keep = .005
        print(f'Keeping only {100.*keep:.1f}% of events for debugging')
        dataset, _ = dataset.split(keep)
    train_dataset, test_dataset = dataset.split(.8)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=shuffle)
    test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=shuffle)

    n_dim_clustering_space = 3
    n_dim_cluster_props = 3

    output_dim = 1+n_dim_clustering_space+n_dim_cluster_props
    model = GravnetModel(input_dim=9, output_dim=output_dim, k=50).to(device)

    # Checkpoint loading
    # if True:
    #     # ckpt = 'ckpts_gravnet_Aug27_2144/ckpt_9.pth.tar'
    #     ckpt = 'ckpts_gravnet_Aug27_0502/ckpt_23.pth.tar'
    #     print(f'Loading initial weights from ckpt {ckpt}')
    #     model.load_state_dict(torch.load(ckpt, map_location=device)['model'])

    epoch_size = len(train_loader.dataset)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5, weight_decay=1e-4)
    scheduler = CyclicLRWithRestarts(optimizer, batch_size, epoch_size, restart_period=400, t_mult=1.1, policy="cosine")

    loss_offset = 1. # To prevent a negative loss from ever occuring

    def loss_fn(out, data, s_c=1., return_components=False):
        device = out.device
        pred_betas = torch.sigmoid(out[:,0])
        pred_cluster_space_coords = out[:,1:1+n_dim_clustering_space]
        assert pred_cluster_space_coords.size(1) == n_dim_clustering_space
        pred_cluster_properties = out[:,1+n_dim_clustering_space:]
        assert pred_cluster_properties.size(1) == n_dim_cluster_props
        assert all(t.device == device for t in [
            pred_betas, pred_cluster_space_coords, data.y,
            data.batch,
            pred_cluster_properties, data.truth_cluster_props
            ])
        out_oc = oc.calc_LV_Lbeta(
            pred_betas,
            pred_cluster_space_coords,
            data.y.long(),
            data.batch,
            return_components=return_components
            )
        out_prop_loss = calc_Lp(
            pred_betas,
            data.y.long(),
            pred_cluster_properties,
            data.truth_cluster_props,
            batch_size,
            return_components
            )
        if return_components:
            out_oc.update(out_prop_loss)
            out_oc['L_total'] = s_c*(out_oc['L_V']+out_oc['L_beta']) + out_oc['L_p'] + loss_offset
            return out_oc
        else:
            L_V, L_beta = out_oc
            L_p = out_prop_loss
            return s_c*(L_V + L_beta) + L_p + loss_offset

    def train(epoch):
        print('Training epoch', epoch)
        model.train()
        scheduler.step()
        try:
            pbar = tqdm.tqdm(train_loader, total=len(train_loader))
            pbar.set_postfix({'loss': '?'})
            for i, data in enumerate(pbar):
                data = data.to(device)
                optimizer.zero_grad()
                result = model(data.x, data.batch)
                loss = loss_fn(result, data)
                loss.backward()
                optimizer.step()
                scheduler.batch_step()
                pbar.set_postfix({'loss': float(loss)})
                # if i == 2: raise Exception
        except Exception:
            print('Exception encountered:', data, ', npzs:')
            print('  ' + '\n  '.join([train_dataset.npzs[int(i)] for i in data.inpz]))
            raise

    def test(epoch):
        N_test = len(test_loader)
        loss_components = {}
        def update(components):
            for key, value in components.items():
                if not key in loss_components: loss_components[key] = 0.
                loss_components[key] += value
        with torch.no_grad():
            model.eval()
            for data in tqdm.tqdm(test_loader, total=len(test_loader)):
                data = data.to(device)
                result = model(data.x, data.batch)
                update(loss_fn(result, data, return_components=True))
        # Divide by number of entries
        for key in loss_components:
            loss_components[key] /= N_test
        # Compute total loss and do printout
        print('test ' + oc.formatted_loss_components_string(loss_components))
        test_loss = loss_components['L_total']
        print(f'Returning {test_loss}')
        return test_loss

    ckpt_dir = strftime('ckpts_gravnet_%b%d_%H%M') if args.ckptdir is None else args.ckptdir
    def write_checkpoint(checkpoint_number=None, best=False):
        ckpt = 'ckpt_best.pth.tar' if best else 'ckpt_{0}.pth.tar'.format(checkpoint_number)
        ckpt = osp.join(ckpt_dir, ckpt)
        if best: print('Saving epoch {0} as new best'.format(checkpoint_number))
        if not args.dry:
            os.makedirs(ckpt_dir, exist_ok=True)
            torch.save(dict(model=model.state_dict()), ckpt)

    min_loss = 1e9
    for i_epoch in range(n_epochs):
        train(i_epoch)
        write_checkpoint(i_epoch)
        test_loss = test(i_epoch)
        if test_loss < min_loss:
            min_loss = test_loss
            write_checkpoint(i_epoch, best=True)

def debug():
    oc.DEBUG = True
    dataset = TauDataset('data/taus')
    dataset.npzs = [
        # 'data/taus/49_nanoML_84.npz',
        # 'data/taus/37_nanoML_4.npz',
        'data/taus/26_nanoML_93.npz',
        # 'data/taus/142_nanoML_75.npz',
        ]
    for data in DataLoader(dataset, batch_size=len(dataset), shuffle=False): break
    print(data.y.sum())
    model = GravnetModel(input_dim=9, output_dim=4)
    with torch.no_grad():
        model.eval()
        out = model(data.x, data.batch)
    pred_betas = torch.sigmoid(out[:,0])
    pred_cluster_space_coords = out[:,1:4]
    out_oc = oc.calc_LV_Lbeta(
        pred_betas,
        pred_cluster_space_coords,
        data.y.long(),
        data.batch.long()
        )

def run_profile():
    from torch.profiler import profile, record_function, ProfilerActivity
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print('Using device', device)

    batch_size = 2
    n_batches = 2
    shuffle = True
    dataset = TauDataset('data/taus')
    dataset.npzs = dataset.npzs[:batch_size*n_batches]
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)
    print(f'Running profiling for {len(dataset)} events, batch_size={batch_size}, {len(loader)} batches')

    model = GravnetModel(input_dim=9, output_dim=8).to(device)
    epoch_size = len(loader.dataset)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-7, weight_decay=1e-4)

    print('Start limited training loop')
    model.train()
    with profile(activities=[ProfilerActivity.CPU], record_shapes=True) as prof:
        with record_function("model_inference"):
            pbar = tqdm.tqdm(loader, total=len(loader))
            pbar.set_postfix({'loss': '?'})
            for i, data in enumerate(pbar):
                data = data.to(device)
                optimizer.zero_grad()
                result = model(data.x, data.batch)
                loss = loss_fn(result, data)
                print(f'loss={float(loss)}')
                loss.backward()
                optimizer.step()
                pbar.set_postfix({'loss': float(loss)})
    print(prof.key_averages().table(sort_by="cpu_time", row_limit=10))
    # Other valid keys:
    # cpu_time, cuda_time, cpu_time_total, cuda_time_total, cpu_memory_usage,
    # cuda_memory_usage, self_cpu_memory_usage, self_cuda_memory_usage, count

if __name__ == '__main__':
    pass
    main()
    # debug()
    # run_profile()