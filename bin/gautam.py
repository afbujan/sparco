#!/usr/bin/env python

from IPython import embed

import time
import glob
import os
import sys

sys.path.append(
    os.path.normpath(os.path.join(os.path.realpath(__file__), '..', '..')))
# TODO fix this-- the issue is that tokyo is not on the load path
sys.path.append(
    os.path.normpath(os.path.join(os.path.realpath(__file__), '..', '..', 'sparco', 'qn')))

import sparco
import sparco.cli
import sparco.mpi as mpi
# from datadb import DB
# from sparse_coder import SparseCoder

args = sparco.cli.parse_args()

C = 64  # num channels
N = 100  # num basis functions
P = 64  # time steps per basis function
T = 128  # time size of 

db = sparco.db.DB(**{
    'dims': (C, N, P, T),
    'channels': range(C),
    'filenames': glob.glob(os.path.join(args.input_directory, '*.h5')),
    'cache': 50, #T*subsample*cache  determines the batch size
    'resample': 2,
    'cull': 0.,
    'maxcull': 5., # (URS) changed 5 to 10 because a lot of times patches were rejected. Changed back: This is a problem with the data being too white
    'std_threshold': 0., # default was 2 but does not work with climate data?
    'subsample': 2, # downsampling 128 1ms to 64 2ms
    'normalize': 'patch',
    'smooth': False,
    'line': False,
    'Fs': 1000
    })

# lam, maxit, niter, target
output_path = os.path.join(args.output_directory,
    "trial{0}".format(time.strftime("%y%m%d%H%M%S")))
ladder = [[0.1,   5,   2000, 5.],
          [0.3, 10,   2000, 2.],
          [0.5, 20,   2000, 2.],
          [0.7, 25,   4000, 1.0],
          [0.9, 30, 10000, 0.5],
          [1.0, 35, 40000, 0.5]]
configs = []

for lam, maxit, num_iterations, target_angle in ladder:
  configs.append({
      'db': db,
      'T': T,
      'batch_size': mpi.procs * 2,
      'num_iterations': num_iterations,
      'target_angle': target_angle,
      'max_angle': target_angle * 2,
      'create_plots': False,
      'inference_settings': {
        'lam': lam,
        'maxit': maxit
        },
      })
initial_eta = .00001
sparse_coder = sparco.sparse_coder.SparseCoder(configs, output_path)
sparse_coder.run(basis_dims=(C,N,P), eta=initial_eta)
