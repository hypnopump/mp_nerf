# Author: Eric Alcaide

import torch
import numpy as np 
from einops import repeat, rearrange

# module
from mp_nerf.massive_pnerf import *
from mp_nerf.utils import *
from mp_nerf.kb_proteins import *
from mp_nerf.proteins import *


def scn_atom_embedd(seq_list):
    """ Returns the token for each atom in the aa seq. 
        Inputs: 
        * seq_list: list of FASTA sequences. same length
    """
    batch_tokens = []
    # do loop in cpu
    for i,seq in enumerate(seq_list):
        batch_tokens.append( torch.tensor([SUPREME_INFO[aa]["atom_token_mask"] \
                                           for aa in seq]) )
    batch_tokens = torch.stack(batch_tokens, dim=0).long()
    return batch_tokens


def chain2atoms(x, mask=None, c=3):
    """ Expand from (L, other) to (L, C, other). """
    wrap = repeat( x, 'l ... -> l c ...', c=c )
    if mask is not None:
        return wrap[mask]
    return wrap


######################
# from: https://static-content.springer.com/esm/art%3A10.1038%2Fs41586-021-03819-2/MediaObjects/41586_2021_3819_MOESM1_ESM.pdf

def rename_symmetric_atoms(pred_coors, true_coors, seq_list, cloud_mask, pred_feats=None): 
    """ Corrects ambiguous atoms (due to 180 torsions - ambiguous sidechains).
        Inputs: 
        * pred_coors: (batch, L, 14, 3) float. sidechainnet format (see mp_nerf.kb_proteins)
        * true_coors: (batch, L, 14, 3) float. sidechainnet format (see mp_nerf.kb_proteins)
        * seq_list: list of FASTA sequences
        * cloud_mask: (batch, L, 14) bool. mask for present atoms
        * pred_feats: (batch, L, 14, D) optional. atom-wise predicted features

        Warning! A coordinate might be missing. TODO:
        Outputs: pred_coors, pred_feats
    """
    aux_cloud_mask = cloud_mask.clone() # will be manipulated

    for i,seq in enumerate(seq_list):
        for aa, pairs in AMBIGUOUS.items():
            # indexes of aas in chain - check coords are given for aa
            amb_idxs  = np.array(pairs["indexs"]).flatten().tolist()
            idxs = torch.tensor([
                k for k,s in enumerate(seq) if s==aa and \
                idx in set( torch.nonzero(aux_cloud_mask[i, :, amb_idxs].sum(dim=-1)).tolist() )
            ]).long()
            # check if any AAs matching
            if idxs.shape[0] == 0: 
                continue 
            # get indexes of non-ambiguous
            aux_cloud_mask[i, idxs, amb_idxs] = False
            non_amb_idx = torch.nonzero(aux_cloud_mask[i, idxs[0]]).tolist()
            for a, pair in enumerate(pairs["indexs"]):
                # calc distances
                d_ij_pred = torch.cdist(pred_coors[ i, idxs, pair ], pred_coors[i, idxs, non_amb_idx], p=2) # 2, N
                d_ij_true = torch.cdist(true_coors[ i, idxs, pair+pair[::-1] ], true_coors[i, idxs, non_amb_idx], p=2) # 2, 2N
                # see if alternative is better (less distance)
                idxs_to_change = ( (d_ij_pred - d_ij_true[2:]).sum(dim=-1) < (d_ij_pred - d_ij_true[:2]).sum(dim=-1) ).nonzero()
                # change those 
                pred_coors[i, idxs[idxs_to_change], pair] = pred_coors[i, idxs[idxs_to_change], pair[::-1]]
                if pred_feats is not None: 
                    pred_feats[i, idxs[idxs_to_change], pair] = pred_feats[i, idxs[idxs_to_change], pair[::-1]]

    return pred_coors, pred_feats 


def torsion_angle_loss(pred_torsions, true_torsions, coeff=2., angle_mask=None): 
    """ Computes a loss on the angles as the cosine of the difference.
        Due to angle periodicity, calculate the disparity on both sides
        Inputs: 
        * pred_torsions: ( (B), L, X ) float. Predicted torsion angles.(-pi, pi)
                                       Same format as sidechainnet. 
        * true_torsions: ( (B), L, X ) true torsion angles. (-pi, pi)
        * coeff: float. weight coefficient
        * angle_mask: ((B), L, (X)) bool. Masks the non-existing angles. 

        Outputs: ( (B), L, 6 ) cosine difference
    """
    l_normal = torch.cos( pred_torsions - true_torsions )
    l_cycle = torch.cos( to_zero_two_pi(pred_torsions) - \
                         to_zero_two_pi(true_torsions) )
    maxi = torch.max( l_normal, l_cycle )
    if angle_mask is not None: 
        maxi[angle_mask] = 1.
    return coeff * (1 - maxi)


def fape_torch(pred_coords, true_coords, max_val=10., l_func=None
               c_alpha=False, seq_list=None, rot_mats_g=None): 
    """ Computes the Frame-Aligned Point Error. Scaled 0 <= FAPE <= 1
        Inputs: 
        * pred_coords: (B, L, C, 3) predicted coordinates. 
        * true_coords: (B, L, C, 3) ground truth coordinates. 
        * max_val: maximum value (it's also the radius due to L1 usage)
        * l_func: function. allow for options other than l1 (consider dRMSD)
        * c_alpha: bool. whether to only calculate frames and loss from c_alphas
        * seq_list: list of strs (FASTA sequences). to calculate rigid bodies' indexs.
                    Defaults to C-alpha if not passed.
        * rot_mats_g: optional. List of n_seqs x (N_frames, 3, 3) rotation matrices.

        Outputs: (B, N_atoms) 
    """
    fape_store = []
    if l_func is not None: 
        l_func = lambda x,y,eps=1e-7,sup=max_val: (((x-y)**2).sum(dim=-1) + eps).sqrt() 
    # for chain
    for s in range(pred_coords.shape[0]):  
        fape_store.append(0)
        cloud_mask = (torch.abs(true_coords[s]).sum(dim=-1) != 0)
        # center both structures
        pred_center = pred_coords[s] - pred_coords[s, cloud_mask].mean(dim=0, keepdim=True)
        true_center = true_coords[s] - true_coords[s, cloud_mask].mean(dim=0, keepdim=True)
        # convert to (B, L*C, 3)
        pred_center = rearrange(pred_center, 'l c d -> (l c) d')
        true_center = rearrange(true_center, 'l c d -> (l c) d')
        mask_center = rearrange(cloud_mask, 'l c -> (l c)')
        # get frames and conversions - same scheme as in mp_nerf proteins' concat of monomers
        if rot_mats_g is None:
            rigid_idxs = scn_rigid_index_mask(seq_list[s], c_alpha=c_alpha)  
            true_frames = get_axis_matrix(*true_center[rigid_idxs].detach(), norm=True)
            pred_frames = get_axis_matrix(*pred_center[rigid_idxs].detach(), norm=True)
            rot_mats  = torch.matmul(torch.transpose(pred_frames, -1, -2), true_frames)
        else: 
            rot_mats = rot_mats_g[s]

        # calculate loss only on c_alphas
        if c_alpha:
            mask_center[:] = False
            mask_center[rigid_idxs[1]] = True

        # measure errors - for residue
        for i,rot_mat in enumerate(rot_mats): 
            fape_store[s] += l1( pred_center[s][mask_center[s]] @ rot_mat, 
                                 true_center[s][mask_center[s]]
                               ).clamp(0, max_val)
        fape_store[s] /= rot_mats.shape[0]            

    # stack and average
    return (1/max_val) * torch.stack(fape_store, dim=0)


# custom

def atom_selector(scn_seq, x, option=None, discard_absent=True): 
    """ Returns a selection of the atoms in a protein. 
        Inputs: 
        * scn_seq: (batch, len) sidechainnet format or list of strings
        * x: (batch, (len * n_aa), dims) sidechainnet format
        * option: one of [torch.tensor, 'backbone-only', 'backbone-with-cbeta',
                  'all', 'backbone-with-oxygen', 'backbone-with-cbeta-and-oxygen']
        * discard_absent: bool. Whether to discard the points for which
                          there are no labels (bad recordings)
    """
    

    # get mask
    present = []
    for i,seq in enumerate(scn_seq): 
        pass_x = x[i] if discard_absent else None
        if pass_x is None and isinstance(seq, torch.Tensor):
            seq = "".join([INDEX2AAS[x] for x in seq.cpu().detach().tolist()])

        present.append( scn_cloud_mask(seq, coords=pass_x) )

    present = torch.stack(present, dim=0).bool()

    
    # atom mask
    if isinstance(option, str):
        atom_mask = torch.tensor([0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        if "backbone" in option: 
            atom_mask[[0, 2]] = 1

        if option == "backbone": 
            pass
        elif option == 'backbone-with-oxygen':
            atom_mask[3] = 1
        elif option == 'backbone-with-cbeta':
            atom_mask[5] = 1
        elif option == 'backbone-with-cbeta-and-oxygen':
            atom_mask[3] = 1
            atom_mask[5] = 1
        elif option == 'all':
            atom_mask[:] = 1
        else: 
            print("Your string doesn't match any option.")
            
    elif isinstance(option, torch.Tensor):
        atom_mask = option
    else:
        raise ValueError('option needs to be a valid string or a mask tensor of shape (14,) ')
    
    mask = rearrange(present * atom_mask.unsqueeze(0).unsqueeze(0).bool(), 'b l c -> b (l c)')
    return x[mask], mask


def noise_internals(seq, angles=None, coords=None, noise_scale=0.5, theta_scale=0.5, verbose=0):
    """ Noises the internal coordinates -> dihedral and bond angles. 
        Inputs: 
        * seq: string. Sequence in FASTA format
        * angles: (l, 11) sidechainnet angles tensor
        * coords: (l, 14, 13)
        * noise_scale: float. std of noise gaussian.
        * theta_scale: float. multiplier for bond angles
        Outputs: 
        * chain (l, c, d)
        * cloud_mask (l, c)
    """
    assert angles is not None or coords is not None, \
           "You must pass either angles or coordinates"
    # get scaffolds
    if angles is None:
        angles = torch.randn(coords.shape[0], 12).to(coords.device)
        
    scaffolds = build_scaffolds_from_scn_angles(seq, angles.clone())
    
    if coords is not None:
        scaffolds = modify_scaffolds_with_coords(scaffolds, coords)
    
    # noise bond angles and dihedrals (dihedrals of everyone, angles only of BB)
    if noise_scale > 0.:
        if verbose: 
            print("noising", noise_scale)
        # thetas (half of noise of dihedrals. only for BB)
        noised_bb = scaffolds["angles_mask"][0, :, :3].clone()
        noised_bb += theta_scale*noise_scale * torch.randn_like(noised_bb) 
        # get noised values between [-pi, pi]
        off_bounds = (noised_bb > 2*np.pi) + (noised_bb < -2*np.pi)
        if off_bounds.sum().item() > 0: 
            noised_bb[off_bounds] = noised_bb[off_bounds] % (2*np.pi)
            
        upper, lower = noised_bb > np.pi, noised_bb < -np.pi 
        if upper.sum().item() > 0:
            noised_bb[upper] = - ( 2*np.pi - noised_bb[upper] ).clone()
        if lower.sum().item() > 0:
            noised_bb[lower] = 2*np.pi + noised_bb[lower].clone()
        scaffolds["angles_mask"][0, :, :3] = noised_bb

        # dihedrals
        noised_dihedrals = scaffolds["angles_mask"][1].clone()
        noised_dihedrals += noise_scale * torch.randn_like(noised_dihedrals)
        # get noised values between [-pi, pi]
        off_bounds = (noised_dihedrals > 2*np.pi) + (noised_dihedrals < -2*np.pi)
        if off_bounds.sum().item() > 0: 
            noised_dihedrals[off_bounds] = noised_dihedrals[off_bounds] % (2*np.pi)
            
        upper, lower = noised_dihedrals > np.pi, noised_dihedrals < -np.pi 
        if upper.sum().item() > 0:
            noised_dihedrals[upper] = - ( 2*np.pi - noised_dihedrals[upper] ).clone()
        if lower.sum().item() > 0:
            noised_dihedrals[lower] = 2*np.pi + noised_dihedrals[lower].clone()
        scaffolds["angles_mask"][1] = noised_dihedrals
    
    # reconstruct
    return protein_fold(**scaffolds)


def combine_noise(true_coords, seq=None, int_seq=None, angles=None,
                  NOISE_INTERNALS=1e-2, INTERNALS_SCN_SCALE=5., 
                  SIDECHAIN_RECONSTRUCT=True):
    """ Combines noises. For internal noise, no points can be missing. 
        Inputs: 
        * true_coords: ((B), N, D)
        * int_seq: (N,) torch long tensor of sidechainnet AA tokens 
        * seq: str of length N. FASTA AAs.
        * angles: (N_aa, D_). optional. used for internal noising
        * NOISE_INTERNALS: float. amount of noise for internal coordinates. 
        * SIDECHAIN_RECONSTRUCT: bool. whether to discard the sidechain and
                                 rebuild by sampling from plausible distro.
        Outputs: (B, N, D) coords and (B, N) boolean mask
    """
    # get seqs right
    assert int_seq is not None or seq is not None, "Either int_seq or seq must be passed"
    if int_seq is not None and seq is None: 
    	seq = "".join([INDEX2AAS[x] for x in int_seq.cpu().detach().tolist()])
    elif int_seq is None and seq is not None: 
    	int_seq = torch.tensor([AAS2INDEX[x] for x in seq.upper()], device=true_coords.device)

    cloud_mask_flat = (true_coords == 0.).sum(dim=-1) != true_coords.shape[-1]
    naive_cloud_mask = scn_cloud_mask(seq).bool()
    
    if NOISE_INTERNALS: 
        assert cloud_mask_flat.sum().item() == naive_cloud_mask.sum().item(), \
               "atoms missing: {0}".format( naive_cloud_mask.sum().item() - \
                                            cloud_mask_flat.sum().item() )
    # expand to batch dim if needed
    if len(true_coords.shape) < 3: 
        true_coords = true_coords.unsqueeze(0)
    noised_coords = true_coords.clone()
    coords_scn = rearrange(true_coords, 'b (l c) d -> b l c d', c=14)

    ###### SETP 1: internals #########
    if NOISE_INTERNALS:
        # create noised and masked noised coords        
        noised_coords, cloud_mask = noise_internals(seq, angles = angles, 
                                                    coords = coords_scn.squeeze(),  
                                                    noise_scale = NOISE_INTERNALS, 
                                                    theta_scale = INTERNALS_SCN_SCALE,
                                                    verbose = False)
        masked_noised = noised_coords[naive_cloud_mask]
        noised_coords = rearrange(noised_coords, 'l c d -> () (l c) d')

    ###### SETP 2: build from backbone #########
    if SIDECHAIN_RECONSTRUCT: 
        bb, mask = atom_selector(int_seq.unsqueeze(0), noised_coords, option="backbone", discard_absent=False)
        scaffolds = build_scaffolds_from_scn_angles(seq, angles=None, device="cpu")
        noised_coords[~mask] = 0.
        noised_coords = rearrange(noised_coords, '() (l c) d -> l c d', c=14)
        noised_coords, _ = sidechain_fold(wrapper = noised_coords.cpu(), **scaffolds, c_beta = False)
        noised_coords = rearrange(noised_coords, 'l c d -> () (l c) d').to(true_coords.device)


    return noised_coords, cloud_mask_flat



if __name__ == "__main__":
    import joblib
    # imports of data (from mp_nerf.utils.get_prot)
    prots = joblib.load("some_route_to_local_serialized_file_with_prots")

    # set params
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # unpack and test
    seq, int_seq, true_coords, angles, padding_seq, mask, pid = prots[-1]

    true_coords = true_coords.unsqueeze(0)

    # check noised internals
    coords_scn = rearrange(true_coords, 'b (l c) d -> b l c d', c=14)
    cloud, cloud_mask = noise_internals(seq, angles=angles, coords=coords_scn[0], noise_scale=1.)
    print("cloud.shape", cloud.shape)

    # check integral
    integral, mask = combine_noise(true_coords, seq=seq, int_seq = None, angles=None,
                                   NOISE_INTERNALS=1e-2, SIDECHAIN_RECONSTRUCT=True)
    print("integral.shape", integral.shape)

    integral, mask = combine_noise(true_coords, seq=None, int_seq = int_seq, angles=None,
                                   NOISE_INTERNALS=1e-2, SIDECHAIN_RECONSTRUCT=True)
    print("integral.shape2", integral.shape)



