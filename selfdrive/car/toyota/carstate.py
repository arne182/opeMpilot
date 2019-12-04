import numpy as np
import math
import selfdrive.messaging as messaging
import selfdrive.messaging_arne as messaging_arne
from common.numpy_fast import interp
from cereal import car
from common.numpy_fast import mean
from common.kalman.simple_kalman import KF1D
from selfdrive.can.can_define import CANDefine
from selfdrive.can.parser import CANParser
from selfdrive.config import Conversions as CV
from selfdrive.car.toyota.values import CAR, DBC, STEER_THRESHOLD, TSS2_CAR, NO_DSU_CAR
from common.travis_checker import travis


GearShifter = car.CarState.GearShifter

def parse_gear_shifter(gear, vals):

  val_to_capnp = {'P': GearShifter.park, 'R': GearShifter.reverse, 'N': GearShifter.neutral,
                  'D': GearShifter.drive, 'B': GearShifter.brake}
  try:
    return val_to_capnp[vals[gear]]
  except KeyError:
    return GearShifter.unknown


def get_can_parser(CP):

  signals = [
    # sig_name, sig_address, default
    ("STEER_ANGLE", "STEER_ANGLE_SENSOR", 0),
    ("GEAR", "GEAR_PACKET", 0),
    ("SPORT_ON", "GEAR_PACKET", 0),
    ("ECON_ON", "GEAR_PACKET", 0),
    ("BRAKE_PRESSED", "BRAKE_MODULE", 0),
    ("BRAKE_PRESSURE", "BRAKE_MODULE", 0),
    ("GAS_PEDAL", "GAS_PEDAL", 0),
    ("WHEEL_SPEED_FL", "WHEEL_SPEEDS", 0),
    ("WHEEL_SPEED_FR", "WHEEL_SPEEDS", 0),
    ("WHEEL_SPEED_RL", "WHEEL_SPEEDS", 0),
    ("WHEEL_SPEED_RR", "WHEEL_SPEEDS", 0),
    ("DOOR_OPEN_FL", "SEATS_DOORS", 1),
    ("DOOR_OPEN_FR", "SEATS_DOORS", 1),
    ("DOOR_OPEN_RL", "SEATS_DOORS", 1),
    ("DOOR_OPEN_RR", "SEATS_DOORS", 1),
    ("SEATBELT_DRIVER_UNLATCHED", "SEATS_DOORS", 1),
    ("TC_DISABLED", "ESP_CONTROL", 1),
    ("STEER_FRACTION", "STEER_ANGLE_SENSOR", 0),
    ("STEER_RATE", "STEER_ANGLE_SENSOR", 0),
    ("CRUISE_ACTIVE", "PCM_CRUISE", 0),
    ("CRUISE_STATE", "PCM_CRUISE", 0),
    ("STEER_TORQUE_DRIVER", "STEER_TORQUE_SENSOR", 0),
    ("STEER_TORQUE_EPS", "STEER_TORQUE_SENSOR", 0),
    ("TURN_SIGNALS", "STEERING_LEVERS", 3),   # 3 is no blinkers
    ("LKA_STATE", "EPS_STATUS", 0),
    ("IPAS_STATE", "EPS_STATUS", 1),
    ("BRAKE_LIGHTS_ACC", "ESP_CONTROL", 0),
    ("AUTO_HIGH_BEAM", "LIGHT_STALK", 0),
    ("DISTANCE_LINES", "PCM_CRUISE_SM", 0),
  ]

  checks = [
    ("BRAKE_MODULE", 40),
    ("GAS_PEDAL", 33),
    ("WHEEL_SPEEDS", 80),
    ("STEER_ANGLE_SENSOR", 80),
    ("PCM_CRUISE", 33),
    ("STEER_TORQUE_SENSOR", 50),
    ("EPS_STATUS", 25),
  ]

  if CP.carFingerprint == CAR.LEXUS_IS:
    signals.append(("MAIN_ON", "DSU_CRUISE", 0))
    signals.append(("SET_SPEED", "DSU_CRUISE", 0))
    checks.append(("DSU_CRUISE", 5))
  else:
    signals.append(("MAIN_ON", "PCM_CRUISE_2", 0))
    signals.append(("SET_SPEED", "PCM_CRUISE_2", 0))
    signals.append(("LOW_SPEED_LOCKOUT", "PCM_CRUISE_2", 0))
    checks.append(("PCM_CRUISE_2", 33))

  if CP.carFingerprint in NO_DSU_CAR:
    signals += [("STEER_ANGLE", "STEER_TORQUE_SENSOR", 0)]

  if CP.carFingerprint == CAR.PRIUS:
    signals += [("STATE", "AUTOPARK_STATUS", 0)]

  # add gas interceptor reading if we are using it
  if CP.enableGasInterceptor:
    signals.append(("INTERCEPTOR_GAS", "GAS_SENSOR", 0))
    signals.append(("INTERCEPTOR_GAS2", "GAS_SENSOR", 0))
    checks.append(("GAS_SENSOR", 50))

  return CANParser(DBC[CP.carFingerprint]['pt'], signals, checks, 0)


def get_cam_can_parser(CP):

  signals = [
    ("TSGN1", "RSA1", 0),
    ("SPDVAL1", "RSA1", 0),
    ("SPLSGN1", "RSA1", 0),
    ("TSGN2", "RSA1", 0),
    ("SPDVAL2", "RSA1", 0),
    ("SPLSGN2", "RSA1", 0),
    ("TSGN3", "RSA2", 0),
    ("SPLSGN3", "RSA2", 0),
    ("TSGN4", "RSA2", 0),
    ("SPLSGN4", "RSA2", 0),
    ("BARRIERS", "LKAS_HUD", 0),
    ("RIGHT_LINE", "LKAS_HUD", 0),
    ("LEFT_LINE", "LKAS_HUD", 0),
    
  ]

  # use steering message to check if panda is connected to frc
  checks = [("STEERING_LKA", 42)]

  return CANParser(DBC[CP.carFingerprint]['pt'], signals, checks, 2)


class CarState():
  def __init__(self, CP):
    # ALCA PARAMS
    # max REAL delta angle for correction vs actuator
    self.CL_MAX_ANGLE_DELTA_BP = [10., 44.]
    self.CL_MAX_ANGLE_DELTA = [1.8, .3]

    # adjustment factor for merging steer angle to actuator; should be over 4; the higher the smoother
    self.CL_ADJUST_FACTOR_BP = [10., 44.]
    self.CL_ADJUST_FACTOR = [16. , 8.]


    # reenrey angle when to let go
    self.CL_REENTRY_ANGLE_BP = [10., 44.]
    self.CL_REENTRY_ANGLE = [5. , 5.]

    # a jump in angle above the CL_LANE_DETECT_FACTOR means we crossed the line
    self.CL_LANE_DETECT_BP = [10., 44.]
    self.CL_LANE_DETECT_FACTOR = [1.5, 1.5]

    self.CL_LANE_PASS_BP = [10., 20., 44.]
    self.CL_LANE_PASS_TIME = [40.,10., 3.] 

    # change lane delta angles and other params
    self.CL_MAXD_BP = [10., 32., 44.]
    self.CL_MAXD_A = [.358, 0.084, 0.042] #delta angle based on speed; needs fine tune, based on Tesla steer ratio of 16.75

    self.CL_MIN_V = 8.9 # do not turn if speed less than x m/2; 20 mph = 8.9 m/s

    # do not turn if actuator wants more than x deg for going straight; this should be interp based on speed
    self.CL_MAX_A_BP = [10., 44.]
    self.CL_MAX_A = [10., 10.] 

    # define limits for angle change every 0.1 s
    # we need to force correction above 10 deg but less than 20
    # anything more means we are going to steep or not enough in a turn
    self.CL_MAX_ACTUATOR_DELTA = 2.
    self.CL_MIN_ACTUATOR_DELTA = 0. 
    self.CL_CORRECTION_FACTOR = 1.

    #duration after we cross the line until we release is a factor of speed
    self.CL_TIMEA_BP = [10., 32., 44.]
    self.CL_TIMEA_T = [0.7 ,0.30, 0.20]

    #duration to wait (in seconds) with blinkers on before starting to turn
    self.CL_WAIT_BEFORE_START = 1
    self.turn_signal_stalk_state = 0
    self.prev_turn_signal_stalk_state = 0
    #END OF ALCA PARAMS

    self.gasbuttonstatus = 0
    self.CP = CP
    self.can_define = CANDefine(DBC[CP.carFingerprint]['pt'])
    self.shifter_values = self.can_define.dv["GEAR_PACKET"]['GEAR']
    self.left_blinker_on = 0
    self.right_blinker_on = 0
    self.angle_offset = 0.
    self.init_angle_offset = False
    self.v_cruise_pcmlast = 41
    self.pcm_acc_status = False
    self.setspeedoffset = 34.0
    self.setspeedcounter = 0
    self.Angles = np.zeros(250)
    self.Angles_later = np.zeros(250)
    self.Angle_counter = 0
    self.Angle = [0, 5, 10, 15,20,25,30,35,60,100,180,270,500]
    self.Angle_Speed = [255,160,100,80,70,60,55,50,40,33,27,17,12]
    if not travis:
      self.arne_pm = messaging_arne.PubMaster(['liveTrafficData', 'arne182Status'])
    # initialize can parser
    self.car_fingerprint = CP.carFingerprint

    # vEgo kalman filter
    dt = 0.01
    # Q = np.matrix([[10.0, 0.0], [0.0, 100.0]])
    # R = 1e3
    self.v_ego_kf = KF1D(x0=[[0.0], [0.0]],
                         A=[[1.0, dt], [0.0, 1.0]],
                         C=[1.0, 0.0],
                         K=[[0.12287673], [0.29666309]])
    self.v_ego = 0.0

  def update(self, cp, cp_cam):
    # update prevs, update must run once per loop
    self.prev_turn_signal_stalk_state = self.turn_signal_stalk_state
    self.prev_left_blinker_on = self.left_blinker_on
    self.prev_right_blinker_on = self.right_blinker_on

    self.door_all_closed = not any([cp.vl["SEATS_DOORS"]['DOOR_OPEN_FL'], cp.vl["SEATS_DOORS"]['DOOR_OPEN_FR'],
                                    cp.vl["SEATS_DOORS"]['DOOR_OPEN_RL'], cp.vl["SEATS_DOORS"]['DOOR_OPEN_RR']])
    self.seatbelt = not cp.vl["SEATS_DOORS"]['SEATBELT_DRIVER_UNLATCHED']

    self.brake_pressed = cp.vl["BRAKE_MODULE"]['BRAKE_PRESSED']
    if self.CP.enableGasInterceptor:
      self.pedal_gas = (cp.vl["GAS_SENSOR"]['INTERCEPTOR_GAS'] + cp.vl["GAS_SENSOR"]['INTERCEPTOR_GAS2']) / 2.
    else:
      self.pedal_gas = cp.vl["GAS_PEDAL"]['GAS_PEDAL']
    self.car_gas = self.pedal_gas
    self.esp_disabled = cp.vl["ESP_CONTROL"]['TC_DISABLED']

    # calc best v_ego estimate, by averaging two opposite corners
    self.v_wheel_fl = cp.vl["WHEEL_SPEEDS"]['WHEEL_SPEED_FL'] * CV.KPH_TO_MS
    self.v_wheel_fr = cp.vl["WHEEL_SPEEDS"]['WHEEL_SPEED_FR'] * CV.KPH_TO_MS
    self.v_wheel_rl = cp.vl["WHEEL_SPEEDS"]['WHEEL_SPEED_RL'] * CV.KPH_TO_MS
    self.v_wheel_rr = cp.vl["WHEEL_SPEEDS"]['WHEEL_SPEED_RR'] * CV.KPH_TO_MS
    v_wheel = mean([self.v_wheel_fl, self.v_wheel_fr, self.v_wheel_rl, self.v_wheel_rr])

    # Kalman filter
    if abs(v_wheel - self.v_ego) > 2.0:  # Prevent large accelerations when car starts at non zero speed
      self.v_ego_kf.x = [[v_wheel], [0.0]]

    self.v_ego_raw = v_wheel
    v_ego_x = self.v_ego_kf.update(v_wheel)
    self.v_ego = float(v_ego_x[0])
    self.a_ego = float(v_ego_x[1])
    self.standstill = not v_wheel > 0.001
    if self.CP.carFingerprint in TSS2_CAR:
      self.angle_steers = cp.vl["STEER_TORQUE_SENSOR"]['STEER_ANGLE']
    elif self.CP.carFingerprint in NO_DSU_CAR:
      # cp.vl["STEER_TORQUE_SENSOR"]['STEER_ANGLE'] is zeroed to where the steering angle is at start.
      # need to apply an offset as soon as the steering angle measurements are both received
      self.angle_steers = cp.vl["STEER_TORQUE_SENSOR"]['STEER_ANGLE'] - self.angle_offset
      angle_wheel = cp.vl["STEER_ANGLE_SENSOR"]['STEER_ANGLE'] + cp.vl["STEER_ANGLE_SENSOR"]['STEER_FRACTION']
      if abs(angle_wheel) > 1e-3 and abs(self.angle_steers) > 1e-3 and not self.init_angle_offset:
        self.init_angle_offset = True
        self.angle_offset = self.angle_steers - angle_wheel
    else:
      self.angle_steers = cp.vl["STEER_ANGLE_SENSOR"]['STEER_ANGLE'] + cp.vl["STEER_ANGLE_SENSOR"]['STEER_FRACTION']
    self.angle_steers_rate = cp.vl["STEER_ANGLE_SENSOR"]['STEER_RATE']
    can_gear = int(cp.vl["GEAR_PACKET"]['GEAR'])
    self.gear_shifter = parse_gear_shifter(can_gear, self.shifter_values)
    try:
      self.econ_on = cp.vl["GEAR_PACKET"]['ECON_ON']
    except:
      self.econ_on = 0
    try:
      self.sport_on = cp.vl["GEAR_PACKET"]['SPORT_ON']
    except:
      self.sport_on = 0
    if self.sport_on == 1:
      self.gasbuttonstatus = 1
    if self.econ_on == 1:
      self.gasbuttonstatus = 2
    if self.sport_on == 0 and self.econ_on == 0:
      self.gasbuttonstatus = 0
    msg = messaging_arne.new_message()
    msg.init('arne182Status')
    msg.arne182Status.gasbuttonstatus = self.gasbuttonstatus
    msg.arne182Status.readdistancelines = cp.vl["PCM_CRUISE_SM"]['DISTANCE_LINES']
    if not travis:
      self.arne_pm.send('arne182Status', msg)
    if self.CP.carFingerprint == CAR.LEXUS_IS:
      self.main_on = cp.vl["DSU_CRUISE"]['MAIN_ON']
    else:
      self.main_on = cp.vl["PCM_CRUISE_2"]['MAIN_ON']
    self.left_blinker_on = cp.vl["STEERING_LEVERS"]['TURN_SIGNALS'] == 1
    self.right_blinker_on = cp.vl["STEERING_LEVERS"]['TURN_SIGNALS'] == 2
    if self.left_blinker_on and not self.right_blinker_on:
      self.turn_signal_stalk_state = -1
    elif self.right_blinker_on and not self.left_blinker_on:
      self.turn_signal_stalk_state = 1
    else:
      self.turn_signal_stalk_state = 0

    # 2 is standby, 10 is active. TODO: check that everything else is really a faulty state
    self.steer_state = cp.vl["EPS_STATUS"]['LKA_STATE']
    self.steer_error = cp.vl["EPS_STATUS"]['LKA_STATE'] not in [1, 5]
    self.ipas_active = cp.vl['EPS_STATUS']['IPAS_STATE'] == 3
    self.brake_error = 0
    self.steer_torque_driver = cp.vl["STEER_TORQUE_SENSOR"]['STEER_TORQUE_DRIVER']
    self.steer_torque_motor = cp.vl["STEER_TORQUE_SENSOR"]['STEER_TORQUE_EPS']
    # we could use the override bit from dbc, but it's triggered at too high torque values
    self.steer_override = abs(self.steer_torque_driver) > STEER_THRESHOLD

    self.user_brake = cp.vl["BRAKE_MODULE"]['BRAKE_PRESSURE']
    if self.CP.carFingerprint == CAR.LEXUS_IS:
      self.v_cruise_pcm = cp.vl["DSU_CRUISE"]['SET_SPEED']
      self.low_speed_lockout = False
    else:
      self.v_cruise_pcm = cp.vl["PCM_CRUISE_2"]['SET_SPEED']
      self.low_speed_lockout = cp.vl["PCM_CRUISE_2"]['LOW_SPEED_LOCKOUT'] == 2

    if cp.vl["PCM_CRUISE"]['CRUISE_STATE'] and not self.pcm_acc_status:
      if self.v_ego < 11.38:
        self.setspeedoffset = max(min(int(41.0-self.v_ego*3.6),34.0),0.0)
        self.v_cruise_pcmlast = self.v_cruise_pcm
      else:
        self.setspeedoffset = 0
        self.v_cruise_pcmlast = self.v_cruise_pcm
    if self.v_cruise_pcm < self.v_cruise_pcmlast:
      if self.setspeedcounter > 0 and self.v_cruise_pcm > 41:
        self.setspeedoffset = self.setspeedoffset + 4
      else:
        if math.floor((int((-self.v_cruise_pcm)*34/128  + 169*34/128)-self.setspeedoffset)/(self.v_cruise_pcm-40)) > 0:
          self.setspeedoffset = self.setspeedoffset + math.floor((int((-self.v_cruise_pcm)*34/128  + 169*34/128)-self.setspeedoffset)/(self.v_cruise_pcm-40))
      self.setspeedcounter = 50
    if self.v_cruise_pcmlast < self.v_cruise_pcm:
      if self.setspeedcounter > 0 and (self.setspeedoffset - 4) > 0:
        self.setspeedoffset = self.setspeedoffset - 4
      else:
        self.setspeedoffset = self.setspeedoffset + math.floor((int((-self.v_cruise_pcm)*34/128  + 169*34/128)-self.setspeedoffset)/(170-self.v_cruise_pcm))
      self.setspeedcounter = 50
    if self.setspeedcounter > 0:
      self.setspeedcounter = self.setspeedcounter - 1
    self.v_cruise_pcmlast = self.v_cruise_pcm
    if int(self.v_cruise_pcm) - self.setspeedoffset < 7:
      self.setspeedoffset = int(self.v_cruise_pcm) - 7
    if int(self.v_cruise_pcm) - self.setspeedoffset > 169:
      self.setspeedoffset = int(self.v_cruise_pcm) - 169
    
      
    self.v_cruise_pcm = min(max(7, int(self.v_cruise_pcm) - self.setspeedoffset),169)

    if not self.left_blinker_on and not self.right_blinker_on:
      self.Angles[self.Angle_counter] = abs(self.angle_steers)
      self.v_cruise_pcm = int(min(self.v_cruise_pcm, interp(np.max(self.Angles), self.Angle, self.Angle_Speed)))
    else:
      self.Angles[self.Angle_counter] = 0
      self.Angles_later[self.Angle_counter] = 0
    self.Angle_counter = (self.Angle_counter + 1 ) % 250
    self.pcm_acc_status = cp.vl["PCM_CRUISE"]['CRUISE_STATE']
    self.pcm_acc_active = bool(cp.vl["PCM_CRUISE"]['CRUISE_ACTIVE'])
    self.brake_lights = bool(cp.vl["ESP_CONTROL"]['BRAKE_LIGHTS_ACC'] or self.brake_pressed)
    if self.CP.carFingerprint == CAR.PRIUS:
      self.generic_toggle = cp.vl["AUTOPARK_STATUS"]['STATE'] != 0
    else:
      self.generic_toggle = bool(cp.vl["LIGHT_STALK"]['AUTO_HIGH_BEAM'])
      
    self.barriers = cp_cam.vl["LKAS_HUD"]['BARRIERS']
    self.rightline = cp_cam.vl["LKAS_HUD"]['RIGHT_LINE']
    self.leftline = cp_cam.vl["LKAS_HUD"]['LEFT_LINE']
    
    
    self.tsgn1 = cp_cam.vl["RSA1"]['TSGN1']
    self.spdval1 = cp_cam.vl["RSA1"]['SPDVAL1']
    
    self.splsgn1 = cp_cam.vl["RSA1"]['SPLSGN1']
    self.tsgn2 = cp_cam.vl["RSA1"]['TSGN2']
    self.spdval2 = cp_cam.vl["RSA1"]['SPDVAL2']
    
    self.splsgn2 = cp_cam.vl["RSA1"]['SPLSGN2']
    self.tsgn3 = cp_cam.vl["RSA2"]['TSGN3']
    self.splsgn3 = cp_cam.vl["RSA2"]['SPLSGN3']
    self.tsgn4 = cp_cam.vl["RSA2"]['TSGN4']
    self.splsgn4 = cp_cam.vl["RSA2"]['SPLSGN4']
    self.noovertake = self.tsgn1 == 65 or self.tsgn2 == 65 or self.tsgn3 == 65 or self.tsgn4 == 65 or self.tsgn1 == 66 or self.tsgn2 == 66 or self.tsgn3 == 66 or self.tsgn4 == 66
    if self.spdval1 > 0 or self.spdval2 > 0:
      dat = messaging_arne.new_message()
      dat.init('liveTrafficData')
      if self.spdval1 > 0:
        dat.liveTrafficData.speedLimitValid = True
        if self.tsgn1 == 36:
          dat.liveTrafficData.speedLimit = self.spdval1 * 1.60934
        elif self.tsgn1 == 1:
          dat.liveTrafficData.speedLimit = self.spdval1
        else:
          dat.liveTrafficData.speedLimit = 0
      else:
        dat.liveTrafficData.speedLimitValid = False
      if self.spdval2 > 0:
        dat.liveTrafficData.speedAdvisoryValid = True
        dat.liveTrafficData.speedAdvisory = self.spdval2
      else:
        dat.liveTrafficData.speedAdvisoryValid = False
      if not travis:
        self.arne_pm.send('liveTrafficData', dat)
