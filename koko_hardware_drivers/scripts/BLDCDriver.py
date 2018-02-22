#!/usr/bin/env python
from comms import BLDCControllerClient
import time
import serial
import math
import signal
import sys
import rospy
import json
from std_msgs.msg import Float64
from std_msgs.msg import Bool
from geometry_msgs.msg import Vector3
from koko_hardware_drivers.msg import MotorState
from comms import *


ENCODER_ANGLE_PERIOD = 1 << 14
MAX_CURRENT = 1.5
MAX_TEMP_WARN = 55 # degrees C
MAX_TEMP_MOTORS_OFF = 75
CONTROL_LOOP_FREQ = 1000

flip_mapping = {}

global command_queue
command_queue = {}
global stop_motors
stop_motors = False

#################################################################################################
############### Helper Functions ################################################################
#################################################################################################

def makeSetCommand(key):
    def setCommand(key, msg):
        global device
        global command_queue
        effort_raw = msg.data
        effort_filtered = effort_raw

        if key in flip_mapping and flip_mapping[key]:
            effort_filtered = -effort_filtered

        if effort_filtered > MAX_CURRENT:
            effort_filtered = MAX_CURRENT
        elif effort_filtered < -MAX_CURRENT:
            effort_filtered = -MAX_CURRENT
        command_queue[key] = effort_filtered
    return lambda msg: setCommand(key, msg)

def stop_motors_cb(msg):
    global stop_motors
    if msg.data:
        stop_motors = True
    else:
        stop_motors = False

#################################################################################################

def main():
    rospy.init_node('jointInterface', anonymous=True)

    motor_ids = rospy.get_param('motor_ids')
    motor_names = rospy.get_param('motor_names')
    mapping = {}

    for id, name in zip(motor_ids, motor_names):
        mapping[id] = name

    global device
    global command_queue
    global stop_motors

    # Set up a signal handler so Ctrl-C causes a clean exit
    def sigintHandler(signal, frame):
        print 'quitting'
        sys.exit()
    signal.signal(signal.SIGINT, sigintHandler)

    # Find and connect to the motor controller
    port = sys.argv[1]
    s = serial.Serial(port=port, baudrate=1000000, timeout=0.1)
    device = BLDCControllerClient(s)

    # Initial hardware setup
    for id in mapping:
        rospy.loginfo("Booting motor %d..." % id)
        device.leaveBootloader(id)
        rospy.sleep(0.1)
        s.flush()
    s.flush()

    calibration_success = False
    for attempt in range(5):
        try:
            # Write calibration values
            for id in mapping:
                rospy.loginfo("Calibrating motor %d..." % id)
                calibrations = device.readCalibration(id)
                device.setZeroAngle(id, calibrations['angle'])
                device.setCurrentControlMode(id)
                device.setInvertPhases(id, calibrations['inv'])
                device.setERevsPerMRev(id, calibrations['epm'])
                device.writeRegisters(id, 0x1022, 1, struct.pack('<f', calibrations['torque']))
            calibration_success = True
            break
        except Exception as e:
            rospy.logwarn(str(e))
            rospy.sleep(0.5)
            pass

    if not calibration_success:
        rospy.logerr("Could not calibrate motors")
        rospy.signal_shutdown("Could not calibrate motors")
        exit()

    rospy.loginfo("Successfully calibrated motors")

    state_publisher = rospy.Publisher("motor_states", MotorState, queue_size=1)
    for key in mapping:
        rospy.Subscriber(mapping[key] + "_cmd", Float64, makeSetCommand(key), queue_size=1)

    # subscriber listens for a stop motor command to set zero current to motors if something goes wrong
    rospy.Subscriber("stop_motors", Bool, stop_motors_cb, queue_size=1)

    angle_start = {}
    for id in mapping:
        angle_start[id] = device.getRotorPosition(id)
        rospy.loginfo("Motor %d startup: supply voltage=%fV", id, device.getVoltage(id))

    r = rospy.Rate(CONTROL_LOOP_FREQ)
    while not rospy.is_shutdown():
        stateMsg = MotorState()
        for key in mapping:
            try:
                if not stop_motors:
                    if key in command_queue:
                        state = device.setCommandAndGetState(key, command_queue.pop(key))
                    else:
                        state = device.getState(key)
                else:
                    state = device.setCommandAndGetState(key, 0.0)

                angle, velocity, direct_current, quadrature_current, \
                        supply_voltage, temperature, accel_x, accel_y, accel_z = state
                angle -= angle_start[key]

                stateMsg.name.append(mapping[key])
                stateMsg.position.append(angle)
                stateMsg.velocity.append(velocity)
                stateMsg.direct_current.append(direct_current)
                stateMsg.quadrature_current.append(quadrature_current)
                stateMsg.supply_voltage.append(supply_voltage)
                stateMsg.temperature.append(temperature)

                accel = Vector3()
                accel.x = accel_x
                accel.y = accel_y
                accel.z = accel_z
                stateMsg.accel.append(accel)

                if temperature > MAX_TEMP_WARN:
                    rospy.logwarn_throttle(1, "Motor {} is overheating, currently at  {}C".format(key, temperature))
                    if temperature > MAX_TEMP_MOTORS_OFF:
                        stop_motors = True
                        rospy.logerr("Motor %d is too hot, setting motor currents to zero", key)

            except Exception as e:
                rospy.logerr("Motor " + str(key) +  " driver error: " + str(e))

        state_publisher.publish(stateMsg)
        r.sleep()

if __name__ == '__main__':
    main()