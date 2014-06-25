"""
A convolution based sparsenet of an array of time series

Key:
 phi - basis [channel, neuron, coefficients]
 A   - coefficients
 X   - patches of data

Note:
1. The kernels are convolution kernels.
"""
from IPython import embed
import csv
import os
import time

import numpy as np

import mpi
import sparco
import sparco.sptools as sptools

class Spikenet(object):
  """
  Settings:
    db: DB. Data source.
    phi: Numpy Array. Must be CxNxP. The initial basis matrix.
    disp: Integer. Iteration interval at which to write basis
    Dimensions:
      C:  Integer. Number channels
      N:  Integer. Number kernels
      P:  Integer. Time points in convolution kernel
      T:  Integer. Time points in raw data (T >> P)
    Inference and learning:
      niter: Integer. Number of iterations for learning
      bs: Integer. Batch size to be divided among procs.
      inference_function: Function.
      inference_settings: Dict. Kwargs for inference_function.
      learner_class: Class. Must implement step(phi, A, X)
      learner_settings: Keyword arguments for learner class constructor.
    Output:
      logger_active:  Boolean. Enable logging.
      log_path: String. Path to log file.
      logger_settings: Keyword arguments for logger initializer.
      echo:  echo stdout to console
      movie:  use movie basis writer (for natural scenes)
      plots:  whether or not to output plots (due to memory leak in matplotlib)
  """

  def __init__(self, **kwargs):
    """Set and validate configuration, initialize output classes."""
    home = os.path.expanduser('~')
    defaults = {
      'db': None,
      'batch_size': 10,
      'num_iterations': 100,
      'run_time_limit': None,
      'phi': None,
      'inference_function': sparco.qn.sparseqn.sparseqn_batch,
      'inference_settings': {
        'lam': 0,
        'maxit': 15,
        'debug': False,
        'positive': False,
        'delta': 0.0001,
        'past': 6
        },
      'eta': .00001,
      'learner_class': sparco.learn.AngleChasingLearner,
      'eta_up_factor': 1.01,
      'eta_down_factor': .99,
      'target_angle': 1.,
      'max_angle': 2.,
      'basis_centering_interval': None,
      'basis_centering_max_shift': None,
      'writer_class': sparco.output.Writer,
      'write_interval': 100,
      'output_path': os.path.join(home, 'sparco_out'),
      'create_plots': True,
      'log_active': False,
      'log_path': os.path.join(home, 'sparco_out', "sparco.log"),
      'basis_method': 1,  # TODO this is a temporary measuer
      # 'logger_settings': {
      #   'echo': True,
      #   }
      }
    settings = sptools.merge(defaults, kwargs)
    for k,v in settings.items():
      setattr(self, k, v)
    # TODO temp for profiling
    self.learn_basis = getattr(self, "learn_basis{0}".format(self.basis_method))
    if mpi.rank == mpi.root:
      self.create_root_buffers = getattr(self, "create_root_buffers{0}".format(self.basis_method))

    self.phi /= sptools.vnorm(self.phi)
    self.patches_per_node = self.batch_size / mpi.procs
    self.update_coeff_statistics_interval = self.write_interval
    sptools.mixin(self, self.learner_class)
    self.a_variance_cumulative = np.zeros(self.phi.shape[1])
    self.run_time =0
    self.last_time = time.time()
    self.validate_configuration()

    C, N, P = self.phi.shape; T = self.db.T
    buffer_dimensions = { 'a': (N, P+T-1), 'x': (C, T), 'xhat': (C,T),
        'dx': (C,T), 'dphi': (C,N,P), 'E': (1,), 'a_l0_norm': (N,),
        'a_l1_norm': (N,), 'a_l2_norm': (N,), 'a_variance': (N,) }
    self.create_node_buffers(buffer_dimensions)
    self.create_root_buffers(buffer_dimensions)

  def create_node_buffers(self, buffer_dimensions):
    nodebufs, nodebufs_mean = {}, {}
    for name,dims in buffer_dimensions.items():
      nodebufs[name] = np.zeros((self.patches_per_node,) + dims)
      nodebufs_mean[name] = np.zeros(dims)
    self.nodebufs = sptools.data(mean=sptools.data(**nodebufs_mean), **nodebufs)

  def create_root_buffers(self, buffer_dimensions):
    for name,dims in buffer_dimensions.items():
      setattr(self.rootbufs, name, None)

  def validate_configuration(self):
    """Throw an exception for any invalid config parameter."""
    # if self.phi.shape != self.basis_dims:
    #   raise ValueError('Warm start phi wrong dimensions')
    if self.batch_size % mpi.procs != 0:
      raise ValueError('Batch size not multiple of number of procs')

### Learning
# methods here draw on methods provided by a learner mixin

  # TODO use a decorator for time termination
  def run(self):
    """ Learn basis by alternative online minimization."""
    self.t = 0
    for self.t in range(self.num_iterations):
      if not self.within_time_limit(): return
      self.iteration() 

  # TODO temp until decorator solution
  def within_time_limit(self):
    now = time.time()
    self.run_time += now - self.last_time
    self.last_time = now
    return self.run_time < self.run_time_limit

  def iteration(self):
    mpi.bcast(self.phi)
    mpi.scatter(self.rootbufs.x, self.nodebufs.x)
    self.infer_coefficients()
    self.learn_basis()
    if self.t > 0 and self.t % self.update_coeff_statistics_interval == 0:
      self.update_coefficient_statistics()

  def infer_coefficients(self):
    for i in range(self.patches_per_node):
      self.nodebufs.a[i] = self.inference_function(self.phi,
          self.nodebufs.x[i], **self.inference_settings)
    # self.a = self.inference_function(self.phi, self.x,
    #   **self.inference_settings)

  # more parallel, higher bandwidth requirement
  def learn_basis1(self):
    self.compute_patch_objectives(self.nodebufs)
    self.average_patch_objectives(self.nodebufs)
    mpi.gather(self.nodebufs.mean.E, self.rootbufs.E)
    mpi.gather(self.nodebufs.mean.dphi, self.rootbufs.dphi)

  # less parallel, lower bandwidth requirement
  def learn_basis2(self):
    mpi.gather(self.nodebufs.a, self.rootbufs.a, mpi.root)

  def compute_patch_objectives(self, bufset):
    for i in range(bufset.x.shape[0]):
      res = sptools.obj(bufset.x[i], bufset.a[i], self.phi)
      bufset.xhat[i], bufset.dx[i] = res[0], res[1]
      bufset.E[i], bufset.dphi[i] = res[2], res[3]

  def average_patch_objectives(self, bufset):
    bufset.mean.dphi = np.mean(bufset.dphi, axis=0)
    bufset.mean.E = np.mean(bufset.E, axis=0)

### Coefficient Statistics

  # TODO see if I can get the normalized norms in a single call
  def update_coefficient_statistics(self):
    for i in range(self.patches_per_node):
      self.nodebufs.a_l0_norm[i] = np.linalg.norm(self.nodebufs.a[i], ord=0, axis=1)
      self.nodebufs.a_l0_norm[i] /= self.nodebufs.a[i].shape[1]

      self.nodebufs.a_l1_norm[i] = np.linalg.norm(self.nodebufs.a[i], ord=1, axis=1)
      self.nodebufs.a_l1_norm[i] /= np.max(self.nodebufs.a_l1_norm[i])

      self.nodebufs.a_l2_norm[i] = np.linalg.norm(self.nodebufs.a[i], ord=2, axis=1)
      self.nodebufs.a_l2_norm[i] /= np.max(self.nodebufs.a_l2_norm[i])

      self.nodebufs.a_variance[i] = np.var(self.nodebufs.a[i], axis=1)
      self.nodebufs.a_variance[i] /= np.max(self.nodebufs.a_variance[i])

    for stat in ['a_l0_norm', 'a_l1_norm', 'a_l1_norm', 'a_variance']:
      setattr(self.nodebufs.mean, stat, np.mean(getattr(self.nodebufs, stat), axis=0))
      mpi.gather(getattr(self.nodebufs.mean, stat), getattr(self.rootbufs, stat))


class RootSpikenet(Spikenet):

  # TODO cleanup logging and profiling; move writer initialization to writer class
  def __init__(self, **kwargs):
    super(RootSpikenet, self).__init__(**kwargs)
    sptools.mixin(self, self.writer_class)
    os.makedirs(self.output_path)
    self.write_configuration(kwargs)
    self.profile_table = csv.DictWriter(
        open(os.path.join(self.output_path, 'profiling.csv'), 'w+'),
        sptools.PROFILING_TABLE.keys())
    self.profile_table.writeheader()
    if self.log_active:
      log_file = open(self.log_settings['path'], 'w+')
      sys.stdout = sparco.output.Logger(sys.stdout, log_file, **self.logger_settings)
      sys.stderr = sparco.output.Logger(sys.stderr, log_file, **self.logger_settings)

  def create_root_buffers1(self, buffer_dimensions):
    rootbufs, rootbufs_mean = {}, {}
    proc_based = list(set(buffer_dimensions.keys()) - set(['x'])) # TODO hack
    for name,dims in buffer_dimensions.items():
      first_dim = mpi.procs if (name in proc_based) else self.batch_size
      rootbufs[name] = np.zeros((first_dim,) + dims)
      rootbufs_mean[name] = np.zeros(dims)
    self.rootbufs = sptools.data(mean=sptools.data(**rootbufs_mean), **rootbufs)

  def create_root_buffers2(self, buffer_dimensions):
    rootbufs, rootbufs_mean = {}, {}
    proc_based = ['a_l0_norm', 'a_l1_norm', 'a_l2_norm', 'a_variance']
    for name,dims in buffer_dimensions.items():
      first_dim = mpi.procs if (name in proc_based) else self.batch_size
      rootbufs[name] = np.zeros((first_dim,) + dims)
      rootbufs_mean[name] = np.zeros(dims)
    self.rootbufs = sptools.data(mean=sptools.data(**rootbufs_mean), **rootbufs)

  def iteration(self):
    self.rootbufs.x = self.db.get_patches(self.batch_size)
    super(RootSpikenet, self).iteration()
    if self.t > 0 and self.write_interval and self.t % self.write_interval == 0:
      self.write_snapshot()

  @sptools.time_track
  def infer_coefficients(self):
    super(RootSpikenet, self).infer_coefficients()

  @sptools.time_track
  def learn_basis1(self):
    super(RootSpikenet, self).learn_basis1()
    self.average_patch_objectives(self.rootbufs)
    self.update_eta_and_phi()

  @sptools.time_track
  def learn_basis2(self):
    super(RootSpikenet, self).learn_basis2()
    self.compute_patch_objectives(self.rootbufs)
    self.average_patch_objectives(self.rootbufs)
    self.update_eta_and_phi()

  def update_eta_and_phi(self):
    self.proposed_phi = sptools.compute_proposed_phi(self.phi,
        self.rootbufs.mean.dphi, self.eta)
    self.phi_angle = sptools.compute_angle(self.phi, self.proposed_phi)
    self.update_phi()
    self.update_eta()

  def update_coefficient_statistics(self):
    super(RootSpikenet, self).update_coefficient_statistics()
    for stat in ['a_l0_norm', 'a_l1_norm', 'a_l2_norm', 'a_variance']:
      mean = np.mean(getattr(self.rootbufs, stat), axis=0)
      setattr(self.rootbufs.mean, stat, mean)
    self.a_variance_cumulative += self.rootbufs.mean.a_variance
    self.basis_sort_order = np.argsort(self.a_variance_cumulative)[::-1]