#!/usr/bin/env python
import zmq
import math
import numpy as np
from common.params import Params
from common.numpy_fast import interp

import selfdrive.messaging as messaging
from cereal import car
from common.realtime import sec_since_boot, DT_PLAN
from selfdrive.swaglog import cloudlog
from selfdrive.config import Conversions as CV
from selfdrive.services import service_list
from selfdrive.controls.lib.speed_smoother import speed_smoother
from selfdrive.controls.lib.longcontrol import LongCtrlState, MIN_CAN_SPEED
from selfdrive.controls.lib.fcw import FCWChecker
from selfdrive.controls.lib.long_mpc import LongitudinalMpc
from selfdrive import op_params
op_params = op_params.opParams()

NO_CURVATURE_SPEED = 200. * CV.MPH_TO_MS

LON_MPC_STEP = 0.2  # first step is 0.2s
MAX_SPEED_ERROR = 2.0
AWARENESS_DECEL = -0.2     # car smoothly decel at .2m/s^2 when user is distracted

# lookup tables VS speed to determine min and max accels in cruise
# make sure these accelerations are smaller than mpc limits
_A_CRUISE_MIN_V  = [-0.8, -0.7, -0.6, -0.5, -0.3]
_A_CRUISE_MIN_BP = [0.0, 5.0, 10.0, 20.0, 55.0]

# need fast accel at very low speed for stop and go
# make sure these accelerations are smaller than mpc limits
_A_CRUISE_MAX_V = [3.5, 3.0, 1.5, .5, .3]
_A_CRUISE_MAX_V_ECO = [1.0, 1.5, 1.0, 0.3, 0.1]
_A_CRUISE_MAX_V_SPORT = [3.5, 3.5, 3.5, 3.5, 3.5]
_A_CRUISE_MAX_V_FOLLOWING = [1.3, 1.6, 1.2, .7, .3]
_A_CRUISE_MAX_BP = [0., 5., 10., 20., 55.]


# Lookup table for turns
_brake_factor = op_params.get("brakefactor", 1.2)
_A_TOTAL_MAX_V = [2.3 * _brake_factor, 3.0 * _brake_factor, 3.9 * _brake_factor]
_A_TOTAL_MAX_BP = [0., 25., 55.]


def calc_cruise_accel_limits(v_ego, following, gasbuttonstatus):
  a_cruise_min = interp(v_ego, _A_CRUISE_MIN_BP, _A_CRUISE_MIN_V)

  if following:
    a_cruise_max = interp(v_ego, _A_CRUISE_MAX_BP, _A_CRUISE_MAX_V_FOLLOWING)
  else:
    if gasbuttonstatus == 1:
      a_cruise_max = interp(v_ego, _A_CRUISE_MAX_BP, _A_CRUISE_MAX_V_SPORT)
    elif gasbuttonstatus == 2:
      a_cruise_max = interp(v_ego, _A_CRUISE_MAX_BP, _A_CRUISE_MAX_V_ECO)
    else:
      a_cruise_max = interp(v_ego, _A_CRUISE_MAX_BP, _A_CRUISE_MAX_V)
  return np.vstack([a_cruise_min, a_cruise_max])


def limit_accel_in_turns(v_ego, angle_steers, a_target, CP, angle_later):
  """
  This function returns a limited long acceleration allowed, depending on the existing lateral acceleration
  this should avoid accelerating when losing the target in turns
  """

  a_total_max = interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y = v_ego**2 * abs(angle_steers) * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_y2 = v_ego**2 * abs(angle_later) * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_x_allowed = a_total_max - a_y
  a_x_allowed2 = a_total_max - a_y2

  a_target[1] = min(a_target[1], a_x_allowed, a_x_allowed2)
  a_target[0] = min(a_target[0], a_target[1])
  #print a_target[1]
  return a_target



class Planner(object):
  def __init__(self, CP, fcw_enabled):
    self.CP = CP
    context = zmq.Context()
    self.poller = zmq.Poller()
    self.lat_Control = messaging.sub_sock(context, service_list['latControl'].port, conflate=True, poller=self.poller)
    
    self.plan = messaging.pub_sock(service_list['plan'].port)
    self.live_longitudinal_mpc = messaging.pub_sock(service_list['liveLongitudinalMpc'].port)

    self.mpc1 = LongitudinalMpc(1, self.live_longitudinal_mpc)
    self.mpc2 = LongitudinalMpc(2, self.live_longitudinal_mpc)

    self.v_acc_start = 0.0
    self.a_acc_start = 0.0

    self.v_acc = 0.0
    self.v_acc_future = 0.0
    self.a_acc = 0.0
    self.v_cruise = 0.0
    self.a_cruise = 0.0

    self.longitudinalPlanSource = 'cruise'
    self.fcw_checker = FCWChecker()
    self.fcw_enabled = fcw_enabled
    
    self.lastlat_Control = None
    
    self.params = Params()

  def choose_solution(self, v_cruise_setpoint, enabled):
    if enabled:
      solutions = {'cruise': self.v_cruise}
      if self.mpc1.prev_lead_status:
        solutions['mpc1'] = self.mpc1.v_mpc
      if self.mpc2.prev_lead_status:
        solutions['mpc2'] = self.mpc2.v_mpc

      slowest = min(solutions, key=solutions.get)

      self.longitudinalPlanSource = slowest

      # Choose lowest of MPC and cruise
      if slowest == 'mpc1':
        self.v_acc = self.mpc1.v_mpc
        self.a_acc = self.mpc1.a_mpc
      elif slowest == 'mpc2':
        self.v_acc = self.mpc2.v_mpc
        self.a_acc = self.mpc2.a_mpc
      elif slowest == 'cruise':
        self.v_acc = self.v_cruise
        self.a_acc = self.a_cruise

    self.v_acc_future = min([self.mpc1.v_mpc_future, self.mpc2.v_mpc_future, v_cruise_setpoint])

  def update(self, sm, CP, VM, PP, live_map_data):
    """Gets called when new radarState is available"""
    cur_time = sec_since_boot()
    v_ego = sm['carState'].vEgo
    
    gasbuttonstatus = 2
    
    long_control_state = sm['controlsState'].longControlState
    v_cruise_kph = sm['controlsState'].vCruise
    force_slow_decel = sm['controlsState'].forceDecel
    v_cruise_setpoint = v_cruise_kph * CV.KPH_TO_MS

    for socket, event in self.poller.poll(0):
      if socket is self.lat_Control:
        self.lastlat_Control = messaging.recv_one(socket).latControl
    
    lead_1 = sm['radarState'].leadOne
    lead_2 = sm['radarState'].leadTwo

    enabled = (long_control_state == LongCtrlState.pid) or (long_control_state == LongCtrlState.stopping)
    following = lead_1.status and lead_1.dRel < 45.0 and lead_1.vLeadK > v_ego and lead_1.aLeadK > 0.0

    v_speedlimit = NO_CURVATURE_SPEED
    v_curvature = NO_CURVATURE_SPEED
    v_speedlimit_ahead = NO_CURVATURE_SPEED

    #map_age = cur_time - rcv_times['liveMapData']
    map_valid = True  # live_map_data.liveMapData.mapValid and map_age < 10.0

    # Speed limit and curvature
    set_speed_limit_active = self.params.get("LimitSetSpeed") == "1" and self.params.get("SpeedLimitOffset") is not None
    if set_speed_limit_active and map_valid:
      offset = float(self.params.get("SpeedLimitOffset"))
      if live_map_data.liveMapData.speedLimitValid:
        speed_limit = live_map_data.liveMapData.speedLimit
        offset = float(self.params.get("SpeedLimitOffset"))
        v_speedlimit = speed_limit + offset
        
      if gasbuttonstatus == 1:
        speed_ahead_distance = 100
      elif gasbuttonstatus == 2:
        speed_ahead_distance = 300
      else:
        speed_ahead_distance = 200
      if live_map_data.liveMapData.speedLimitAheadValid and live_map_data.liveMapData.speedLimitAheadDistance < speed_ahead_distance:
        speed_limit_ahead = live_map_data.liveMapData.speedLimitAhead
        v_speedlimit_ahead = speed_limit_ahead + offset
        
      if live_map_data.liveMapData.curvatureValid:
        curvature = abs(live_map_data.liveMapData.curvature)
        a_y_max = 2.975 - v_ego * 0.0375  # ~1.85 @ 75mph, ~2.6 @ 25mph
        v_curvature = math.sqrt(a_y_max / max(1e-4, curvature)) / 1.3 * _brake_factor
        v_curvature = min(NO_CURVATURE_SPEED, v_curvature)

    decel_for_turn = bool(v_curvature < min([v_cruise_setpoint, v_speedlimit, v_ego + 1.]))
    v_cruise_setpoint = min([v_cruise_setpoint, v_curvature, v_speedlimit, v_speedlimit_ahead])

    # Calculate speed for normal cruise control
    if enabled:
      accel_limits = map(float, calc_cruise_accel_limits(v_ego, following, gasbuttonstatus))
      if gasbuttonstatus == 0:
        accellimitmaxdynamic = -0.0018*v_ego+0.2
        jerk_limits = [min(-0.1, accel_limits[0]), max(accellimitmaxdynamic, accel_limits[1])]  # dynamic
      elif gasbuttonstatus == 1:
        accellimitmaxsport = -0.002*v_ego+0.4
        jerk_limits = [min(-0.25, accel_limits[0]), max(accellimitmaxsport, accel_limits[1])]  # sport
      elif gasbuttonstatus == 2:
        accellimitmaxeco = -0.0015*v_ego+0.1
        jerk_limits = [min(-0.1, accel_limits[0]), max(accellimitmaxeco, accel_limits[1])]  # eco

      if not sm['carState'].leftBlinker and not sm['carState'].rightBlinker:
        steering_angle = sm['carState'].steeringAngle
        if self.lastlat_Control and v_ego > 11:      
          angle_later = self.lastlat_Control.anglelater
        else:
          angle_later = 0
      else:
        angle_later = 0
        steering_angle = 0
      accel_limits = limit_accel_in_turns(v_ego, steering_angle, accel_limits, self.CP, angle_later * self.CP.steerRatio)

      if force_slow_decel:
        # if required so, force a smooth deceleration
        accel_limits[1] = min(accel_limits[1], AWARENESS_DECEL)
        accel_limits[0] = min(accel_limits[0], accel_limits[1])

      # Change accel limits based on time remaining to turn
      if decel_for_turn:
        time_to_turn = max(1.0, live_map_data.liveMapData.distToTurn / max((v_ego + v_curvature)/2, 1.))
        required_decel = min(0, (v_curvature - v_ego) / time_to_turn)*0.85
        accel_limits[0] = max(accel_limits[0], required_decel)

      if v_speedlimit_ahead < v_speedlimit:
        if live_map_data.liveMapData.speedLimitAheadDistance != 0:
          required_decel = min(0, (v_speedlimit_ahead*v_speedlimit_ahead - v_ego*v_ego)/(live_map_data.liveMapData.speedLimitAheadDistance*2))
        required_decel = max(required_decel*0.85, -3.0)
        accel_limits[0] = min(accel_limits[0], required_decel)

      self.v_cruise, self.a_cruise = speed_smoother(self.v_acc_start, self.a_acc_start,
                                                    v_cruise_setpoint,
                                                    accel_limits[1], accel_limits[0],
                                                    jerk_limits[1], jerk_limits[0],
                                                    LON_MPC_STEP)
      # cruise speed can't be negative even is user is distracted
      self.v_cruise = max(self.v_cruise, 0.)
    else:
      starting = long_control_state == LongCtrlState.starting
      a_ego = min(sm['carState'].aEgo, 0.0)
      reset_speed = MIN_CAN_SPEED if starting else v_ego
      reset_accel = self.CP.startAccel if starting else a_ego
      self.v_acc = reset_speed
      self.a_acc = reset_accel
      self.v_acc_start = reset_speed
      self.a_acc_start = reset_accel
      self.v_cruise = reset_speed
      self.a_cruise = reset_accel

    self.mpc1.set_cur_state(self.v_acc_start, self.a_acc_start)
    self.mpc2.set_cur_state(self.v_acc_start, self.a_acc_start)

    self.mpc1.update(sm['carState'], lead_1, v_cruise_setpoint)
    self.mpc2.update(sm['carState'], lead_2, v_cruise_setpoint)

    self.choose_solution(v_cruise_setpoint, enabled)

    # determine fcw
    if self.mpc1.new_lead:
      self.fcw_checker.reset_lead(cur_time)

    blinkers = sm['carState'].leftBlinker or sm['carState'].rightBlinker
    fcw = self.fcw_checker.update(self.mpc1.mpc_solution, cur_time, v_ego, sm['carState'].aEgo,
                                  lead_1.dRel, lead_1.vLead, lead_1.aLeadK,
                                  lead_1.yRel, lead_1.vLat,
                                  lead_1.fcw, blinkers) and not sm['carState'].brakePressed
    if fcw:
      cloudlog.info("FCW triggered %s", self.fcw_checker.counters)

    radar_dead = not sm.alive['radarState']

    radar_errors = list(sm['radarState'].radarErrors)
    radar_fault = car.RadarData.Error.fault in radar_errors
    radar_can_error = car.RadarData.Error.canError in radar_errors

    # **** send the plan ****
    plan_send = messaging.new_message()
    plan_send.init('plan')

    plan_send.valid = sm.all_alive_and_valid(service_list=['carState', 'controlsState', 'radarState'])

    plan_send.plan.mdMonoTime = sm.logMonoTime['model']
    plan_send.plan.radarStateMonoTime = sm.logMonoTime['radarState']

    # longitudal plan
    plan_send.plan.vCruise = self.v_cruise
    plan_send.plan.aCruise = self.a_cruise
    plan_send.plan.vStart = self.v_acc_start
    plan_send.plan.aStart = self.a_acc_start
    plan_send.plan.vTarget = self.v_acc
    plan_send.plan.aTarget = self.a_acc
    plan_send.plan.vTargetFuture = self.v_acc_future
    plan_send.plan.hasLead = self.mpc1.prev_lead_status
    plan_send.plan.longitudinalPlanSource = self.longitudinalPlanSource

    plan_send.plan.vCurvature = v_curvature
    plan_send.plan.decelForTurn = decel_for_turn
    plan_send.plan.mapValid = map_valid

    radar_valid = not (radar_dead or radar_fault)
    plan_send.plan.radarValid = bool(radar_valid)
    plan_send.plan.radarCanError = bool(radar_can_error)

    plan_send.plan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.rcv_time['radarState']

    # Send out fcw
    fcw = fcw and (self.fcw_enabled or long_control_state != LongCtrlState.off)
    plan_send.plan.fcw = fcw

    self.plan.send(plan_send.to_bytes())

    # Interpolate 0.05 seconds and save as starting point for next iteration
    a_acc_sol = self.a_acc_start + (DT_PLAN / LON_MPC_STEP) * (self.a_acc - self.a_acc_start)
    v_acc_sol = self.v_acc_start + DT_PLAN * (a_acc_sol + self.a_acc_start) / 2.0
    self.v_acc_start = v_acc_sol
    self.a_acc_start = a_acc_sol
