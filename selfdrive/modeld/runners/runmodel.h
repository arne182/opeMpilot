#ifndef RUNMODEL_H
#define RUNMODEL_H

class RunModel {
public:
  RunModel(const char *path, float *output, size_t output_size, int runtime);
  ~RunModel();
  virtual void addRecurrent(float *state, int state_size) {}
  virtual void addDesire(float *state, int state_size) {}
  virtual void addTrafficConvention(float *state, int state_size) {}
  virtual void execute(float *net_input_buf, int buf_size, bool trafficd = false) {}
};

#endif

