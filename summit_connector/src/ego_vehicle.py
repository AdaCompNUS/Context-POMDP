#!/usr/bin/env python2

from summit import Summit
import carla

import random
import math
import numpy as np
import sys

import rospy
import tf
from util import *

from geometry_msgs.msg import Twist, Pose, Point, Quaternion, Vector3, Polygon, Point32, PoseStamped
from nav_msgs.msg import Odometry
from nav_msgs.msg import Path as NavPath
from std_msgs.msg import Float32, Bool, Int32
from msg_builder.msg import car_info as CarInfo  # panpan
import tf2_geometry_msgs
import tf2_ros
import geometry_msgs
import time
from tf import TransformListener
import tf.transformations as tftrans


change_left = -1
remain = 0
change_right = 1
VEHICLE_STEER_KP = 2.5

''' ========== UTILITY FUNCTIONS AND CLASSES ========== '''

class NetworkAgentPath:
    def __init__(self, sumo_network, min_points, interval):
        self.sumo_network = sumo_network
        self.min_points = min_points
        self.interval = interval
        self.route_points = []

    @staticmethod
    def rand_path(sumo_network, min_points, interval, segment_map, min_safe_points=None, rng=random):
        if min_safe_points is None:
            min_safe_points = min_points

        spawn_point = None
        route_paths = None
        while not spawn_point or len(route_paths) < 1:
            spawn_point = segment_map.rand_point()
            spawn_point = sumo_network.get_nearest_route_point(spawn_point)
            route_paths = sumo_network.get_next_route_paths(spawn_point, min_safe_points - 1, interval)

        path = NetworkAgentPath(sumo_network, min_points, interval)
        path.route_points = rng.choice(route_paths)[0:min_points]
        return path

    def resize(self, rng=random):
        while len(self.route_points) < self.min_points:
            next_points = self.sumo_network.get_next_route_points(self.route_points[-1], self.interval)
            if len(next_points) == 0:
                return False
            self.route_points.append(rng.choice(next_points))
        return True

    def get_min_offset(self, position):
        min_offset = None
        for i in range(len(self.route_points) / 2):
            route_point = self.route_points[i]
            offset = position - self.sumo_network.get_route_point_position(route_point)
            offset = offset.length() 
            if min_offset == None or offset < min_offset:
                min_offset = offset
        return min_offset

    def cut(self, position):
        cut_index = 0
        min_offset = None
        min_offset_index = None
        for i in range(len(self.route_points) / 2):
            route_point = self.route_points[i]
            offset = position - self.sumo_network.get_route_point_position(route_point)
            offset = offset.length() 
            if min_offset == None or offset < min_offset:
                min_offset = offset
                min_offset_index = i
            if offset <= 1.0:
                cut_index = i + 1

        # Invalid path because too far away.
        if min_offset > 1.0:
            self.route_points = self.route_points[min_offset_index:]
        else:
            self.route_points = self.route_points[cut_index:]

    def get_position(self, index=0):
        return self.sumo_network.get_route_point_position(self.route_points[index])

    def get_yaw(self, index=0):
        pos = self.sumo_network.get_route_point_position(self.route_points[index])
        next_pos = self.sumo_network.get_route_point_position(self.route_points[index + 1])
        return np.rad2deg(math.atan2(next_pos.y - pos.y, next_pos.x - pos.x))



class EgoVehicle(Summit):
    def __init__(self):
        super(EgoVehicle, self).__init__()

        # Initialize fields.
        self.gamma_cmd_accel = 0
        self.gamma_cmd_steer = 0
        self.gamma_cmd_speed = 0
        self.pp_cmd_steer = 0
        self.pomdp_cmd_accel = 0
        self.pomdp_cmd_steer = 0
        self.pomdp_cmd_speed = 0
        self.lane_decision = 0
        self.speed_control_last_update = None
        self.speed_control_integral = 0.0
        self.speed_control_last_error = 0.0

        self.start_time = None
        self.last_decision = remain

        # ROS stuff.
        self.control_mode = rospy.get_param('~control_mode', 'gamma')
        self.speed_control_mode = rospy.get_param('~speed_control', 'vel')
        self.gamma_max_speed = rospy.get_param('~gamma_max_speed', 6.0)

        print('Ego_vehicle control mode: {}'.format(self.control_mode))
        print('Ego_vehicle speed mode: {}'.format(self.speed_control_mode))
        sys.stdout.flush()

        rospy.Subscriber('/pomdp_cmd_accel', Float32, self.pomdp_cmd_accel_callback, queue_size=1)
        rospy.Subscriber('/pomdp_cmd_steer', Float32, self.pomdp_cmd_steer_callback, queue_size=1)
        rospy.Subscriber('/pomdp_cmd_speed', Float32, self.pomdp_cmd_speed_callback, queue_size=1)

        self.pp_cmd_accel_sub = rospy.Subscriber('/purepursuit_cmd_steer',
                                                 Float32, self.pp_cmd_steer_callback, queue_size=1)

        self.odom_broadcaster = tf.TransformBroadcaster()
        self.odom_pub = rospy.Publisher('/odom', Odometry, queue_size=1)
        self.car_info_pub = rospy.Publisher('/ego_state', CarInfo, queue_size=1)
        self.plan_pub = rospy.Publisher('/plan', NavPath, queue_size=1)
        self.ego_dead_pub = rospy.Publisher('/ego_dead', Bool, queue_size=1)

        # Create path.
        self.actor = None
        self.speed = 0.0
        while self.actor is None:
            self.path = NetworkAgentPath.rand_path(
                    self.sumo_network, 50, 1.0, self.sumo_network_spawn_segments,
                    min_safe_points=100, rng=self.rng)

            vehicle_bp = self.rng.choice(self.world.get_blueprint_library().filter('vehicle.mini.cooperst'))
            vehicle_bp.set_attribute('role_name', 'ego_vehicle')
            spawn_position = self.path.get_position()
            spawn_trans = carla.Transform()
            spawn_trans.location.x = spawn_position.x
            spawn_trans.location.y = spawn_position.y
            spawn_trans.location.z = 0.5
            spawn_trans.rotation.yaw = self.path.get_yaw()

            print("Ego-vehicle at {} {}".format(spawn_position.x, spawn_position.y))
            sys.stdout.flush()

            self.actor = self.world.try_spawn_actor(vehicle_bp, spawn_trans)

            # if self.actor:
            #    self.actor.set_collision_enabled(True)

        self.world.wait_for_tick(1.0)  # Wait for collision to be applied.
        actor_physics_control = self.actor.get_physics_control()
        self.steer_angle_range = \
            (actor_physics_control.wheels[0].max_steer_angle + actor_physics_control.wheels[1].max_steer_angle) / 2
        
        time.sleep(1)  # wait for the vehicle to drop
        self.publish_odom()
        self.publish_il_car_info()
        self.publish_plan()

        self.broadcaster = None
        self.publish_odom_transform()
        self.transformer = TransformListener()

        self.update_timer = rospy.Timer(rospy.Duration(1.0 / 20), self.update)

    def dispose(self):
        self.update_timer.shutdown()
        self.actor.destroy()

    def get_position(self):
        location = self.actor.get_location()
        if location.z < 0:
            self.ego_dead_pub.publish(True)
        return carla.Vector2D(location.x, location.y)

    def get_cur_ros_pose(self):
        cur_pose = geometry_msgs.msg.PoseStamped()

        cur_pose.header.stamp = rospy.Time.now()
        cur_pose.header.frame_id = "/map"

        cur_pose.pose.position.x = self.actor.get_location().x
        cur_pose.pose.position.y = self.actor.get_location().y
        cur_pose.pose.position.z = self.actor.get_location().z

        quat = tf.transformations.quaternion_from_euler(
            float(0), float(0), float(np.deg2rad(self.actor.get_transform().rotation.yaw)))
        cur_pose.pose.orientation.x = quat[0]
        cur_pose.pose.orientation.y = quat[1]
        cur_pose.pose.orientation.z = quat[2]
        cur_pose.pose.orientation.w = quat[3]

        return cur_pose

    def get_cur_ros_transform(self):
        transformStamped = geometry_msgs.msg.TransformStamped()

        transformStamped.header.stamp = rospy.Time.now()
        transformStamped.header.frame_id = "map"
        transformStamped.child_frame_id = 'odom'

        transformStamped.transform.translation.x = self.actor.get_location().x
        transformStamped.transform.translation.y = self.actor.get_location().y
        transformStamped.transform.translation.z = self.actor.get_location().z

        quat = tf.transformations.quaternion_from_euler(
            float(0), float(0), float(
                np.deg2rad(self.actor.get_transform().rotation.yaw)))
        transformStamped.transform.rotation.x = quat[0]
        transformStamped.transform.rotation.y = quat[1]
        transformStamped.transform.rotation.z = quat[2]
        transformStamped.transform.rotation.w = quat[3]

        return transformStamped

    def get_transform_wrt_odom_frame(self):
        try:
            (trans, rot) = self.transformer.lookupTransform("map", "odom", rospy.Time(0.2))
        except:
            return None

        cur_pose = self.get_cur_ros_pose()

        transform = tftrans.concatenate_matrices(
            tftrans.translation_matrix(trans), tftrans.quaternion_matrix(rot))
        inversed_transform = tftrans.inverse_matrix(transform)

        inv_translation = tftrans.translation_from_matrix(inversed_transform)
        inv_quaternion = tftrans.quaternion_from_matrix(inversed_transform)

        transformStamped = geometry_msgs.msg.TransformStamped()
        transformStamped.transform.translation.x = inv_translation[0]
        transformStamped.transform.translation.y = inv_translation[1]
        transformStamped.transform.translation.z = inv_translation[2]
        transformStamped.transform.rotation.x = inv_quaternion[0]
        transformStamped.transform.rotation.y = inv_quaternion[1]
        transformStamped.transform.rotation.z = inv_quaternion[2]
        transformStamped.transform.rotation.w = inv_quaternion[3]

        cur_transform_wrt_odom = tf2_geometry_msgs.do_transform_pose(
            cur_pose, transformStamped)

        translation = cur_transform_wrt_odom.pose.position

        quaternion = (
            cur_transform_wrt_odom.pose.orientation.x,
            cur_transform_wrt_odom.pose.orientation.y,
            cur_transform_wrt_odom.pose.orientation.z,
            cur_transform_wrt_odom.pose.orientation.w)

        _, _, yaw = tf.transformations.euler_from_quaternion(quaternion)

        return translation, yaw

    def update_gamma_control(self):
        gamma = carla.RVOSimulator()

        gamma_id = 0
        for (i, actor) in enumerate(self.world.get_actors()):
            if isinstance(actor, carla.Vehicle):
                if actor.attributes['number_of_wheels'] == 2:
                    type_tag = 'Bicycle'
                else:
                    type_tag = 'Car'
                bounding_box_corners = get_vehicle_bounding_box_corners(actor)
            elif isinstance(actor, carla.Walker):
                type_tag = 'People'
                bounding_box_corners = get_pedestrian_bounding_box_corners(actor)
            else:
                continue

            gamma.add_agent(carla.AgentParams.get_default(type_tag), gamma_id)
            gamma.set_agent_position(gamma_id, get_position(actor))
            gamma.set_agent_velocity(gamma_id, get_velocity(actor))
            gamma.set_agent_heading(gamma_id, get_forward_direction(actor))
            gamma.set_agent_bounding_box_corners(gamma_id, bounding_box_corners)
            gamma.set_agent_pref_velocity(gamma_id, get_velocity(actor))
            gamma_id += 1
           
        ego_id = gamma_id
        gamma.add_agent(carla.AgentParams.get_default('Car'), ego_id)
        gamma.set_agent_position(ego_id, get_position(self.actor))
        gamma.set_agent_velocity(ego_id, get_velocity(self.actor))
        gamma.set_agent_heading(ego_id, get_forward_direction(self.actor))
        gamma.set_agent_bounding_box_corners(ego_id, get_vehicle_bounding_box_corners(self.actor))
        target_position = self.path.get_position(5)
        pref_vel = self.gamma_max_speed * (target_position - get_position(self.actor)).make_unit_vector()
        gamma.set_agent_pref_velocity(ego_id, pref_vel)

        gamma.do_step()
        target_vel = gamma.get_agent_velocity(ego_id)
                    
        self.gamma_cmd_speed = target_vel.length()
        self.gamma_cmd_steer = np.clip(
                np.clip(
                    VEHICLE_STEER_KP * get_signed_angle_diff(target_vel, get_forward_direction(self.actor)), 
                    -45.0, 45.0) / self.steer_angle_range,
                -1.0, 1.0)


    def publish_odom_transform(self):
        self.broadcaster = tf2_ros.StaticTransformBroadcaster()
        static_transformStamped = self.get_cur_ros_transform()
        self.broadcaster.sendTransform(static_transformStamped)

    def publish_odom(self):
        # Check if result available.
        result = self.get_transform_wrt_odom_frame()
        if result is None:
            return

        current_time = rospy.Time.now()

        frame_id = "odom"
        child_frame_id = "base_link"

        (translation, yaw) = result
        pos = carla.Location(translation.x, translation.y, translation.z)
        vel = self.actor.get_velocity()
        v_2d = np.array([vel.x, vel.y, 0])
        forward = np.array([math.cos(yaw), math.sin(yaw), 0])
        speed = np.vdot(forward, v_2d)
        odom_quat = tf.transformations.quaternion_from_euler(0, 0, yaw)
        w_yaw = self.actor.get_angular_velocity().z

        self.odom_broadcaster.sendTransform(
            (pos.x, pos.y, pos.z),
            odom_quat,
            current_time,
            child_frame_id,
            frame_id
        )

        odom = Odometry()
        odom.header.stamp = current_time
        odom.header.frame_id = frame_id
        # get pos and yaw w.r.t. the map frame
        pos = self.actor.get_location()
        yaw = np.deg2rad(self.actor.get_transform().rotation.yaw)
        odom_quat = tf.transformations.quaternion_from_euler(0, 0, yaw)
        odom.pose.pose = Pose(Point(pos.x, pos.y, 0), Quaternion(*odom_quat))
        odom.child_frame_id = child_frame_id
        odom.twist.twist = Twist(Vector3(vel.x, vel.y, vel.z), Vector3(0, 0, w_yaw))
        self.odom_pub.publish(odom)

    def publish_il_car_info(self):
        car_info_msg = CarInfo()

        pos = self.actor.get_location()
        pos2D = carla.Vector2D(pos.x, pos.y)
        vel = self.actor.get_velocity()
        yaw = np.deg2rad(self.actor.get_transform().rotation.yaw)
        v_2d = np.array([vel.x, vel.y, 0])
        forward = np.array([math.cos(yaw), math.sin(yaw), 0])
        speed = np.vdot(forward, v_2d)
        self.speed = speed

        car_info_msg.id = self.actor.id
        car_info_msg.car_pos.x = pos.x
        car_info_msg.car_pos.y = pos.y
        car_info_msg.car_pos.z = pos.z
        car_info_msg.car_yaw = yaw
        car_info_msg.car_speed = speed
        car_info_msg.car_steer = self.actor.get_control().steer
        car_info_msg.car_vel.x = vel.x
        car_info_msg.car_vel.y = vel.y
        car_info_msg.car_vel.z = vel.z

        car_info_msg.car_bbox = Polygon()
        corners = get_bounding_box_corners(self.actor)
        for corner in corners:
            car_info_msg.car_bbox.points.append(Point32(
                x=corner.x, y=corner.y, z=0.0))

        wheels = self.actor.get_physics_control().wheels
        # TODO I think that CARLA might have forgotten to divide by 100 here.
        wheel_positions = [w.position / 100 for w in wheels]

        front_axle_center = (wheel_positions[0] + wheel_positions[1]) / 2
        rear_axle_center = (wheel_positions[2] + wheel_positions[3]) / 2

        car_info_msg.front_axle_center.x = front_axle_center.x
        car_info_msg.front_axle_center.y = front_axle_center.y
        car_info_msg.front_axle_center.z = front_axle_center.z
        car_info_msg.rear_axle_center.x = rear_axle_center.x
        car_info_msg.rear_axle_center.y = rear_axle_center.y
        car_info_msg.rear_axle_center.z = rear_axle_center.z
        car_info_msg.max_steer_angle = wheels[0].max_steer_angle

        self.car_info_pub.publish(car_info_msg)

    def publish_plan(self):
        current_time = rospy.Time.now()

        gui_path = NavPath()
        gui_path.header.frame_id = 'map'
        gui_path.header.stamp = current_time

        values = [(carla.Vector2D(self.actor.get_location().x, self.actor.get_location().y),
                   self.actor.get_transform().rotation.yaw)]
        # Exclude last point because no yaw information.
        values += [(self.path.get_position(i), self.path.get_yaw(i)) for i in range(len(self.path.route_points) - 1)]
        for (position, yaw) in values:
            pose = PoseStamped()
            pose.header.frame_id = 'map'
            pose.header.stamp = current_time
            pose.pose.position.x = position.x
            pose.pose.position.y = position.y
            pose.pose.position.z = 0
            quaternion = tf.transformations.quaternion_from_euler(0, 0, np.deg2rad(yaw))
            pose.pose.orientation.x = quaternion[0]
            pose.pose.orientation.y = quaternion[1]
            pose.pose.orientation.z = quaternion[2]
            pose.pose.orientation.w = quaternion[3]
            gui_path.poses.append(pose)

        self.plan_pub.publish(gui_path)

    def pomdp_cmd_accel_callback(self, accel):
        self.pomdp_cmd_accel = accel.data

    def pomdp_cmd_steer_callback(self, steer):
        self.pomdp_cmd_steer = steer.data

    def pomdp_cmd_speed_callback(self, speed):
        self.pomdp_cmd_speed = speed.data

    def pp_cmd_steer_callback(self, steer):
        self.pp_cmd_steer = steer.data

    def gamma_lane_change_decision_callback(self, decision):
        sys.stdout.flush()

        self.lane_decision = int(decision.data)
        if self.lane_decision == self.last_decision:
            return
        if self.lane_decision * self.last_decision == -1:
            self.last_decision = self.lane_decision
            return
        # print('change lane decision {}'.format(self.lane_decision))
        # sys.stdout.flush()
        self.last_decision = self.lane_decision
        # self.update_path(self.lane_decision)

    def draw_path(self, path):
        color_i = 255
        last_loc = None
        for i in range(len(path.route_points)):
            pos = path.get_position(i)
            loc = carla.Location(pos.x, pos.y, 0.1)
            if last_loc is not None:
                self.world.debug.draw_line(last_loc, loc, life_time=0.1,
                                           color=carla.Color(color_i, color_i, 0, 0))
            last_loc = carla.Location(pos.x, pos.y, 0.1)

    def send_control_from_vel(self):
        control = self.actor.get_control()
        if self.control_mode == 'gamma':
            cmd_speed = self.gamma_cmd_speed
            cmd_steer = self.gamma_cmd_steer
            kp, ki, kd, k, discount = 0.3, 0.1, 0.005, 1.0, 1.0
        else:
            cmd_speed = self.pomdp_cmd_speed
            cmd_steer = self.pp_cmd_steer
            # kp, ki, kd, k = 1.8, 1.0, 0.3, 0.3
            kp, ki, kd, k, discount = 1.2, 0.5, 0.2, 0.8, 0.99

        cur_speed = self.actor.get_velocity()
        cur_speed = (cur_speed.x ** 2 + cur_speed.y ** 2) ** 0.5

        if False:
            print('cur_speed {} {}'.format(cur_speed, time.time() - script_start))
            print('cmd_speed {} {}'.format(cmd_speed, time.time() - script_start))
            sys.stdout.flush()

        if cmd_speed < 1e-5 and cur_speed < 0.5:
            control.throttle = 0
            control.brake = 1.0
            control.hand_brake = True

            self.speed_control_last_update = None
            self.speed_control_integral = 0.0
            self.speed_control_last_error = 0.0
        else:
            cur_time = rospy.Time.now()

            if self.speed_control_last_update is None:
                dt = 0.0
            else:
                dt = (cur_time - self.speed_control_last_update).to_sec()

            speed_error = cmd_speed - cur_speed
            self.speed_control_integral = speed_error * dt + discount * self.speed_control_integral

            speed_control = kp * speed_error + ki * self.speed_control_integral
            if self.speed_control_last_update is not None:
                speed_control += kd * (speed_error - self.speed_control_last_error) / dt
            speed_control = k * speed_control

            self.speed_control_last_update = cur_time
            self.speed_control_last_error = speed_error

            if speed_control >= 0:
                control.throttle = speed_control
                control.brake = 0.0
                control.hand_brake = False
            else:
                control.throttle = 0.0
                control.brake = -speed_control
                control.hand_brake = False

        control.steer = np.clip(cmd_steer * 45.0 / self.steer_angle_range, -1.0, 1.0)

        control.manual_gear_shift = True
        control.gear = 1

        self.actor.apply_control(control)

    def send_control_from_acc(self):
        # Calculate control and send to CARLA.
        # print("controlling vehicle with acc={} cur_vel={}".format(self.cmd_accel, self.speed))
        control = self.actor.get_control()

        if self.control_mode == 'gamma':
            cmd_accel = self.gamma_cmd_accel
            cmd_steer = self.gamma_cmd_steer
        elif self.control_mode == 'joint_pomdp':
            cmd_accel = self.pomdp_cmd_accel
            cmd_steer = self.pomdp_cmd_steer
        elif self.control_mode == 'other':
            cmd_accel = self.pomdp_cmd_accel
            cmd_steer = self.pp_cmd_steer

        control.steer = cmd_steer
        if cmd_accel > 0:
            control.throttle = cmd_accel
            control.brake = 0.0
        elif cmd_accel == 0:
            control.throttle = 0.0
            control.brake = 0.0
        else:
            control.throttle = 0
            control.brake = 1.0
        control.manual_gear_shift = True
        control.gear = 1

        self.actor.apply_control(control)

    def update_path(self, lane_decision):
        if lane_decision == remain:
            return

        pos = self.actor.get_location()
        ego_veh_pos = carla.Vector2D(pos.x, pos.y)
        yaw = np.deg2rad(self.actor.get_transform().rotation.yaw)

        forward_vec = carla.Vector2D(math.cos(yaw), math.sin(yaw))
        sidewalk_vec = forward_vec.rotate(np.deg2rad(90))  # rotate clockwise by 90 degree

        ego_veh_pos_in_new_lane = None
        if lane_decision == change_left:
            ego_veh_pos_in_new_lane = ego_veh_pos - 4.0 * sidewalk_vec
        else:
            ego_veh_pos_in_new_lane = ego_veh_pos + 4.0 * sidewalk_vec

        cur_route_point = self.sumo_network.get_nearest_route_point(
            self.path.get_position(0))
        new_route_point = self.sumo_network.get_nearest_route_point(ego_veh_pos_in_new_lane)

        lane_change_probability = 1.0
        if new_route_point.edge == cur_route_point.edge and new_route_point.lane != cur_route_point.lane:
            if self.rng.uniform(0.0, 1.0) <= lane_change_probability:
                new_path_candidates = self.sumo_network.get_next_route_paths(new_route_point, self.path.min_points - 1,
                                                                        self.path.interval)
                new_path = NetworkAgentPath(self, self.path.min_points, self.path.interval)
                new_path.route_points = self.rng.choice(new_path_candidates)[0:self.path.min_points]
                self.path = new_path

    def update(self, event):
        
        # Publish info.
        if not self.path.resize():
            self.ego_dead_pub.publish(True)
            return
        else:
            self.path.cut(self.get_position())
            if not self.path.resize():
                self.ego_dead_pub.publish(True)
                return

        if self.control_mode == 'gamma':
            self.update_gamma_control()

        if self.speed_control_mode == 'acc':
            self.send_control_from_acc()
        elif self.speed_control_mode == 'vel':
            self.send_control_from_vel()

        # self.draw_path(self.path)
        self.publish_odom()
        self.publish_il_car_info()
        self.publish_plan()


if __name__ == '__main__':
    script_start = time.time()
    rospy.init_node('ego_vehicle')
    init_time = rospy.Time.now()

    ego_vehicle = EgoVehicle()
    rospy.on_shutdown(ego_vehicle.dispose)
    rospy.spin()
