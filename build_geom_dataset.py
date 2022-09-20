import argparse
import os
import pickle
import random
import sys

import numpy as np
import rdkit
import torch
import tqdm
from rdkit import Chem
from rdkit.Chem import QED, AllChem, Descriptors, Descriptors3D, RDConfig
from torch.utils.data import (BatchSampler, DataLoader, Dataset,
                              SequentialSampler)

sys.path.append(os.path.join(RDConfig.RDContribDir, 'SA_Score'))
import sascorer

from qm9.data import collate as qm9_collate


def extract_properties(mol_3D, smi):
    mol = Chem.MolFromSmiles(smi)
    properties = {}
    properties['qed'] = QED.qed(mol)
    properties['logp'] = Descriptors.MolLogP(mol)
    properties['sas'] = sascorer.calculateScore(mol)
    properties['Asphericity'] = Descriptors3D.Asphericity(mol_3D)
    return properties

def extract_conformers(args):
    base_dir = os.path.join(args.data_dir, args.data_file)
    drugs_file = os.listdir(base_dir)
    save_file = f"geom_drugs_{'no_h_' if args.remove_h else ''}{args.conformations}_random"
    smiles_list_file = 'geom_drugs_smiles_random.txt'
    number_atoms_file = f"geom_drugs_n_{'no_h_' if args.remove_h else ''}{args.conformations}_random_prop"


    all_smiles = []
    all_number_atoms = []
    dataset_conformers = []
    mol_id = 0
    for i, drugs_1k in enumerate(tqdm.tqdm(drugs_file)):
        with open(os.path.join(base_dir, drugs_1k), 'rb') as f:
            if os.path.join(base_dir, drugs_1k)[-6:] != 'pickle':
                continue
            drug_pkl = pickle.load(f)
            all_smiles.append(drug_pkl['smiles'])
            conformers = drug_pkl['conformers']
            # Get the energy of each conformer. Keep only the lowest values
            all_energies = []
            for conformer in conformers:
                all_energies.append(conformer['totalenergy'])
            all_energies = np.array(all_energies)
            argsort = np.argsort(all_energies)
            #lowest_energies = argsort[:args.conformations]
            lowest_energies = list(range(0, len(conformers)))
            random.shuffle(lowest_energies)
            lowest_energies  = lowest_energies[:args.conformations]
            for id in lowest_energies:
                conformer = conformers[id]
                properties = np.array(list(extract_properties(conformer['rd_mol'], drug_pkl['smiles']).values()))
                coords = np.array([conformer['rd_mol'].GetConformer().GetAtomPosition(x) for x in range(conformer['rd_mol'].GetNumAtoms())]).astype(float)        # n x 3
                atom_type = np.array([atom.GetAtomicNum() for atom in conformer['rd_mol'].GetAtoms()]).astype(float)
                atom_type = np.expand_dims(atom_type, 1)
                coords = np.hstack((atom_type, coords))
                if args.remove_h:
                    mask = coords[:, 0] != 1.0
                    coords = coords[mask]
                n = coords.shape[0]
                all_number_atoms.append(n)
                mol_id_arr = mol_id * np.ones((n, 1), dtype=float)
                properties_array = properties * np.ones((n, 1), dtype=float)
                id_coords = np.hstack((mol_id_arr, coords, properties_array))

                dataset_conformers.append(id_coords)
                mol_id += 1

    print("Total number of conformers saved", mol_id)
    all_number_atoms = np.array(all_number_atoms)
    dataset = np.vstack(dataset_conformers)

    print("Total number of atoms in the dataset", dataset.shape[0])
    print("Average number of atoms per molecule", dataset.shape[0] / mol_id)

    # Save conformations
    np.save(os.path.join(args.output_dir, save_file), dataset)
    # Save SMILES
    with open(os.path.join(args.output_dir, smiles_list_file), 'w') as f:
        for s in all_smiles:
            f.write(s)
            f.write('\n')

    # Save number of atoms per conformation
    np.save(os.path.join(args.output_dir, number_atoms_file), all_number_atoms)
    print("Dataset processed.")


def load_split_data(args, conformation_file, val_proportion=0.1, test_proportion=0.1,
                    filter_size=None):
    from pathlib import Path
    path = Path(conformation_file)
    base_path = path.parent.absolute()

    # base_path = os.path.dirname(conformation_file)
    all_data = np.load(conformation_file)  # 2d array: num_atoms x 5

    mol_id = all_data[:, 0].astype(int)
    conformers = all_data[:, 1:]
    # Get ids corresponding to new molecules
    split_indices = np.nonzero(mol_id[:-1] - mol_id[1:])[0] + 1
    data_list = np.split(conformers, split_indices)

    # Filter based on molecule size.
    if filter_size is not None:
        # Keep only molecules <= filter_size
        data_list = [molecule for molecule in data_list
                     if molecule.shape[0] <= filter_size]

        assert len(data_list) > 0, 'No molecules left after filter.'

    # CAREFUL! Only for first time run:
    #set random seed
    #np.random.seed(2022)
    #perm = np.random.permutation(len(data_list)).astype('int32')
    # print('Warning, currently taking a random permutation for '
    #       'train/val/test partitions, this needs to be fixed for'
    #       'reproducibility.')
    #assert not os.path.exists(os.path.join(args.output_dir, 'geom_permutation.npy'))
    #np.save(os.path.join(args.output_dir, 'geom_permutation.npy'), perm)
    # del perm

    perm = np.load(os.path.join(args.output_dir, 'geom_permutation.npy'))
    data_list = [data_list[i] for i in perm if i < len(data_list)]

    num_mol = len(data_list)
    val_index = int(num_mol * val_proportion)
    test_index = val_index + int(num_mol * test_proportion)
    val_data, test_data, train_data = np.split(data_list, [val_index, test_index])
    return train_data, val_data, test_data


class GeomDrugsDataset(Dataset):
    def __init__(self, data_list, transform=None):
        """
        Args:
            transform (callable, optional): Optional transform to be applied
                on a sample.
        """
        self.transform = transform

        # Sort the data list by size
        lengths = [s.shape[0] for s in data_list]
        argsort = np.argsort(lengths)               # Sort by decreasing size
        self.data_list = [data_list[i] for i in argsort]
        # Store indices where the size changes
        self.split_indices = np.unique(np.sort(lengths), return_index=True)[1][1:]

    def __len__(self):
        return len(self.data_list)

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        sample = self.data_list[idx]
        if self.transform:
            sample = self.transform(sample)
        return sample


class CustomBatchSampler(BatchSampler):
    """ Creates batches where all sets have the same size. """
    def __init__(self, sampler, batch_size, drop_last, split_indices):
        super().__init__(sampler, batch_size, drop_last)
        self.split_indices = split_indices

    def __iter__(self):
        batch = []
        for idx in self.sampler:
            batch.append(idx)
            if len(batch) == self.batch_size or idx + 1 in self.split_indices:
                yield batch
                batch = []
        if len(batch) > 0 and not self.drop_last:
            yield batch

    def __len__(self):
        count = 0
        batch = 0
        for idx in self.sampler:
            batch += 1
            if batch == self.batch_size or idx + 1 in self.split_indices:
                count += 1
                batch = 0
        if batch > 0 and not self.drop_last:
            count += 1
        return count


def collate_fn(batch):
    batch = {prop: qm9_collate.batch_stack([mol[prop] for mol in batch])
             for prop in batch[0].keys()}

    atom_mask = batch['atom_mask']

    # Obtain edges
    batch_size, n_nodes = atom_mask.size()
    edge_mask = atom_mask.unsqueeze(1) * atom_mask.unsqueeze(2)

    # mask diagonal
    diag_mask = ~torch.eye(edge_mask.size(1), dtype=torch.bool,
                           device=edge_mask.device).unsqueeze(0)
    edge_mask *= diag_mask

    # edge_mask = atom_mask.unsqueeze(1) * atom_mask.unsqueeze(2)
    batch['edge_mask'] = edge_mask.view(batch_size * n_nodes * n_nodes, 1)

    return batch


class GeomDrugsDataLoader(DataLoader):
    def __init__(self, sequential, dataset, batch_size, shuffle, drop_last=False):

        if sequential:
            # This goes over the data sequentially, advantage is that it takes
            # less memory for smaller molecules, but disadvantage is that the
            # model sees very specific orders of data.
            assert not shuffle
            sampler = SequentialSampler(dataset)
            batch_sampler = CustomBatchSampler(sampler, batch_size, drop_last,
                                               dataset.split_indices)
            super().__init__(dataset, batch_sampler=batch_sampler)

        else:
            # Dataloader goes through data randomly and pads the molecules to
            # the largest molecule size.
            super().__init__(dataset, batch_size, shuffle=shuffle,
                             collate_fn=collate_fn, drop_last=drop_last)


class GeomDrugsTransform(object):
    def __init__(self, dataset_info, include_charges, device, sequential):
        self.atomic_number_list = torch.Tensor(dataset_info['atomic_nb'])[None, :]
        self.device = device
        self.include_charges = include_charges
        self.sequential = sequential

    def __call__(self, data):
        n = data.shape[0]
        new_data = {}
        #modify for context_nf = 1
        new_data['positions'] = torch.from_numpy(data[:, 1:4])
        atom_types = torch.from_numpy(data[:, 0].astype(int)[:, None])
        one_hot = atom_types == self.atomic_number_list
        new_data['one_hot'] = one_hot
        new_data['context'] = torch.from_numpy(data[:, 5:6])
        if self.include_charges:
            new_data['charges'] = torch.zeros(n, 1, device=self.device)
        else:
            new_data['charges'] = torch.zeros(0, device=self.device)
        new_data['atom_mask'] = torch.ones(n, device=self.device)

        if self.sequential:
            edge_mask = torch.ones((n, n), device=self.device)
            edge_mask[~torch.eye(edge_mask.shape[0], dtype=torch.bool)] = 0
            new_data['edge_mask'] = edge_mask.flatten()
        return new_data


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--conformations", type=int, default=4,
                        help="Max number of conformations kept for each molecule.")
    parser.add_argument("--remove_h", default=True, help="Remove hydrogens from the dataset.")
    parser.add_argument("--data_dir", type=str, default='/sharefs/sharefs-qb/3D_jtvae/GEOM/')
    parser.add_argument("--output_dir", type=str, default='/sharefs/sharefs-syx/qb_data/EDM')
    parser.add_argument("--data_file", type=str, default="rdkit_folder/drugs")
    args = parser.parse_args()
    extract_conformers(args)
