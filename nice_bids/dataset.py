from typing import List, Union, Tuple

import pandas as pd
import json
import os
from glob import glob
import re
import shutil
from zipfile import ZipFile

from functools import partial
from multiprocessing import cpu_count
from tqdm.contrib.concurrent import process_map

from nice_bids.paths import BIDSPath, EEGPath, DerivativePath

def load_path(data:Tuple[str, dict]) -> EEGPath:
    filepath, metadata = data
    return EEGPath(
        root=filepath,
        metadata=metadata
    )

def load_derivative(data:Tuple[str, dict]) -> DerivativePath:
    filepath, metadata = data
    return DerivativePath(
        root=filepath,
        metadata=metadata
    )

# TODO(Lao): Accept list of sub, ses, task and acq as coma separated strings to allow to only load some parts of the datasets
# If I do this, the split dataloader could use this functionality to split the subjects and then construct two separated datasets 
# from there
class NICEBIDS:

    def __init__(self, root:str, 
                 sub:Union[str,List[str]]=None,
                 ses:Union[str,List[str]]=None,
                 task:Union[str,List[str]]=None,
                 acq:Union[int,List[int]]=None,
                 derivatives:List[str]=None, rjust:int=2) -> None:

        self.subset = {
            'sub': sub,
            'ses': ses,
            'task': task,
            'acq': acq
        }

        # Filter and parse list string and single values to list
        self.subset = {k:v for k,v in self.subset.items() if v is not None}
        self.subset = {
            k: v.replace(', ',',').split(',') if isinstance(v,str) else v 
            for k,v in self.subset.items()
        }

        # build subset regex pattern for filtering
        subs = '(' + '|'.join(self.subset.get('sub', ['.+'])) + ')'
        sess = '(' + '|'.join(self.subset.get('ses', ['.+'])) + ')'
        tasks = '(' + '|'.join(self.subset.get('task', ['.+'])) + ')'
        acqs = self.subset.get('acq', ['.+'])
        if acqs[0] != '.+':
            acqs = [str(acq).rjust(self.rjust, '0') for acq in acqs]
        acqs = '(' + '|'.join(acqs) + ')'
        self.subset_regex_pattern = os.path.join(
            f'sub-{subs}',
            f'ses-{sess}',
            'eeg',
            f'sub-{subs}_ses-{sess}_task-{tasks}_acq-{acqs}_*.*$' # <- match end of string
        )
        
        self.rjust = rjust
        self.root = root
        self.participants_descriptions = None
        self.participants = None

        self._read_participants()
        self._read_files()
        self._read_derivatives(derivatives)
        self._create_metadata()

    def _read_participants(self):
        print('Reading participants metadata')
        self.participants = pd.read_csv(
            os.path.join(self.root, 'participants.tsv'),
            sep='\t',
            index_col='participant_id',
            encoding='utf8'
        )

        # Remove participants not selected
        if 'sub' in self.subset:
            subs = [f'sub-{sub}' for sub in self.subset['sub']]
            fileter_mask = self.participants.index.isin(subs)
            self.participants = self.participants[fileter_mask]

        participants_json = os.path.join(self.root, 'participants.json')
        if os.path.exists(participants_json):
            with open(participants_json, 'r') as json_file:
                self.participants_descriptions = json.load(json_file)

    def _read_files(self, n_jobs=None):
        print('Loading data')
        
        files = glob(os.path.join(
            self.root,
            f'sub-*',
            f'ses-*',
            'eeg',
            f'sub-*_ses-*_task-*_eeg.*'
        ))

        # Filter incorrect recording filenames
        files = filter(BIDSPath.correct_filepath, files)
        
        # Filter by selected subset
        pattern = os.path.join(self.root, self.subset_regex_pattern)
        files = [file for file in files if re.match(pattern, file)]
        files = [
            (file, re.search('sub-([a-zA-Z0-9]+)/', file).group(1))
            for file in files
        ]
        files = [
            (file, self.participants.loc[f'sub-{sub}'].to_dict())
            for file, sub in files
        ]

        # Parallel loading with a progress bar
        self.files = process_map(
            load_path,
            files,
            max_workers=n_jobs if n_jobs is not None else cpu_count(),
            chunksize=1,
            leave=False
        )

    def _read_derivatives(self, derivatives:List[str]=None, n_jobs:int=None):
        derivative_root = os.path.join(self.root, 'derivatives')
        if not os.path.exists(derivative_root):
            print('There is no derivatives folder. Reading derivative skiped')
            return

        print('Reading derivatives')

        files = glob(os.path.join(
            derivative_root,
            '*',
            f'sub-*',
            f'ses-*',
            'eeg',
            f'sub-*_ses-*_task-*_*.*'
        ))

        # Filter derivatives by subset and folders
        derivatives = '(' + '|'.join(derivatives) + ')' if derivatives is not None else '(.+)'
        pattern = os.path.join(
            derivative_root,
            derivatives,
            self.subset_regex_pattern
        )

        derivative_files = [file for file in files if re.match(pattern, file)]

        derivative_files = [
            file for file in derivative_files
            if BIDSPath.correct_filepath(file, 'derivative')
        ]

        derivative_files = [
            (file, re.search('sub-([a-zA-Z0-9]+)/', file).group(1))
            for file in derivative_files
        ]
        derivative_files = [
            (file, self.participants.loc[f'sub-{sub}'].to_dict())
            for file, sub in derivative_files
        ]

        # Parallel loading with a progress bar
        self.derivative_files = process_map(
            load_derivative,
            derivative_files,
            max_workers=n_jobs if n_jobs is not None else cpu_count(),
            chunksize=1,
            leave=False
        )

    def _create_metadata(self):

        if len(self.files) == 0:
            print('No files found, no metadata to merge')
            return

        print('Creating metadata')
        self.metadata = pd.DataFrame(
            [file.metadata for file in self.files]
        )

        grouping_cols = ['participant_id', 'ses', 'task', 'acq']

        for rec, group in self.metadata.groupby(grouping_cols):
            if len(group) == 1 and group['run'].values[0] != 1:
                raise ValueError(f'Single files metadata incorrect runs != 1 {rec}')

            t_dup = group.duplicated(subset=[c for c in group.columns if c != 'run'], keep=False).sum()
            if t_dup != len(group) and len(group) > 1:
                raise ValueError(f'Files metadata inconsistent across runs {rec}')

        self.metadata.drop_duplicates(
            subset=grouping_cols,
            keep='first',
            ignore_index=True,
            inplace=True
        )

        self.metadata = self.metadata.reindex(
            [*grouping_cols, *[c for c in self.metadata.columns if c not in grouping_cols]],
            axis=1
        )

        for c in self.metadata.columns:
            if 'date' in c:
                self.metadata[c] = pd.to_datetime(
                    self.metadata[c],
                    errors='ignore'
                )

    def get(self, sub:str=None, task:str=None, ext:str=None, ses:str=None,
                  acq:int=None, run:int=None, suffix:str=None):
        
        query = partial(
            NICEBIDS.query_filter,
            sub=sub, task=task, ext=ext, ses=ses,
            acq=acq, run=run, suffix=suffix
        )
        
        return [file for file in self.files if query(file)]
    
    def to_df(self, sub:str=None, task:str=None, ses:str=None,
                    acq:int=None, run:int=None):

        res = self.metadata.copy()

        fields_to_filter = {
            'participant_id': f'sub-{sub}' if sub is not None else None,
            'task': task,
            'ses': ses,
            'acq': acq,
            'run': run
        }

        for (field_name, val) in fields_to_filter.items():
            if val is not None:
                res = res[res[field_name] == val]
        
        return res

    #TODO(Lao): Accept lists of suffix, ext, sub, ses, task, acq and run to filer subsets
    def get_derivatives(self, derivative:str, 
                        suffix:str=None, ext:str=None,
                        sub:str=None, ses:str=None,
                        task:str=None, acq:int=None, run:int=None):
        
        query = partial(
            NICEBIDS.query_filter,
            sub=sub, task=task, ext=ext, ses=ses,
            acq=acq, run=run, suffix=suffix, derivative=derivative
        )

        return [file for file in self.derivative_files if query(file)]

    def __repr__(self) -> str:
        return f'Subjects: {len(self.participants)}, files: {len(self.files)}'

    def __iter__(self):
        return iter(self.files)
    
    def __getitem__(self, idx:int):
        return self.files[idx]

    def __len__(self):
        return len(self.files)

    def add(self, zip_file, sub:str, ses:str, task:str, acq:str, setup:str):
        
        
        # TODO: Check all files in the zip_file have correct name pattern

        # Check there is not data for that subject, session, task, acquisition
        found_files = dataset.get(sub=sub, ses=ses, task=task, acq=acq)
        if len(found_files) != 0:
            raise ValueError(f'Data already exists for {sub} {ses} {task} {acq}')

        # Copy file from current location to the new location of the
        # database building subdirectories if needed
        new_file = EEGPath(
            root=self.root,
            sub=sub,
            ses=ses,
            task=task,
            acq=acq,
            ext='zip'
        )
        new_file_folder = new_file.path.parent
        new_file_folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(zip_file, new_file.path)

        # extract zip file
        ZipFile(new_file.path).extractall(
            path=new_file.path.parent,
            members=None,
            pwd=None
        )

        # remove zip file
        os.remove(new_file.path)

        # Add metadata to participants.tsv
        self.participants.loc[f'sub-{sub}', task] = True 
        self.participants.to_csv(
            os.path.join(self.root, 'participants.tsv'),
            sep='\t',
            encoding='utf8'
        )

        # Add metadata to sidecar    
        with open(new_file._build_sidecar_path(), 'w') as json_file:
            json.dump({'setup': setup}, json_file, indent=4)

    @staticmethod
    def query_filter(
        file, sub:str, task:str, ext:str,
        ses:str, acq:str, run:str, suffix:str, derivative:str=None):

        zipped_fields = zip(
            ['sub', 'task', 'ses', 'acq', 'run'],
            [sub, task, ses, acq, run]
        )

        correct_fields = all([
            v is None or f'{k}-{v}' in str(file)
            for k, v in zipped_fields
        ])

        correct_suffix = suffix is None or f'_{suffix}.' in str(file)

        correct_ext = ext is None or str(file).endswith(f'.{ext}')

        correct_derivative = file.derivative == derivative 

        return (
            correct_fields
            and correct_suffix
            and correct_ext
            and correct_derivative
        )
