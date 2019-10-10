import os
import json
import time


def write_params(params_file, params):
  try:
    with open(params_file, "w") as f:
      json.dump(params, f, indent=2, sort_keys=True)
    os.chmod(params_file, 0o764)
  except IOError:
    pass


def read_params(params_file, default_params):
  try:
    with open(params_file, "r") as f:
      params = json.load(f)
    return params, True
  except:
    params = default_params
    return params, False


class opParams:
  def __init__(self):
    self.params_file = "/data/op_params.json"
    self.kegman_file = "/data/kegman.json"
    self.params = {}
    self.read_status = False
    self.last_read_time = time.time()
    self.read_timeout = 1.0  # max frequency to read with self.put(...) (s)
    self.default_params = {'cameraOffset': 0.06, 'wheelTouchFactor': 10, 'speed_offset': 0, 'osm': True}
    self.run_init()  # restores, reads, and updates params

  def add_default_params(self, force_update=False):
    prev_params = dict(self.params)
    for i in self.default_params:
      if force_update:
        self.params.update({i: self.default_params[i]})
      elif i not in self.params:
        self.params.update({i: self.default_params[i]})
    return prev_params == self.params

  def run_init(self):  # does first time initializing of default params, and/or restoring from kegman.json
    force_update = False  # replaces values with default params if True, not just add add missing key/value pairs
    self.params = self.default_params  # in case any file is corrupted
    to_write = False
    no_params = False
    if os.path.isfile(self.params_file):
      self.params, self.read_status = read_params(self.params_file, self.default_params)
      if read_status:
        to_write = not self.add_default_params(force_update=force_update)  # if new default data has been added
      else:  # don't overwrite corrupted params, just print to screen
        print("ERROR: Can't read op_params.json file")
    elif os.path.isfile(self.kegman_file):
      to_write = True  # write no matter what
      try:
        with open(self.kegman_file, "r") as f:  # restore params from kegman
          self.params = json.load(f)
          self.add_default_params(force_update=force_update)
      except:
        print("ERROR: Can't read kegman.json file")
    else:
      no_params = True  # user's first time running a fork with kegman_conf or op_params
    if to_write or no_params:
      write_params(self.params_file, self.params)

  def put(self, key, value):
    self.params.update({key: value})
    write_params(self.params_file, self.params)

  def get(self, key=None, default=None):  # can specify a default value if key doesn't exist
    if (time.time() - self.last_read_time) >= self.read_timeout:  # make sure we aren't reading file too often
      self.params, self.read_status = read_params(self.params_file, self.default_params)
      self.last_read_time = time.time()
    if key is None:  # get all
      return self.params
    else:
      if self.read_status:
        return self.params[key] if key in self.params else default
      else:
        return default
