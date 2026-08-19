#!/usr/bin/env python3
"""Play a recorded route using Nav2 and replay recorded implement actions."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import rclpy
import yaml
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateThroughPoses
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from std_msgs.msg import Float32, Int8, String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener


class RoutePlayerNode(Node):
    """Send a recorded route to Nav2 and reproduce recorded tool states by waypoint."""

    def __init__(self) -> None:
        super().__init__('route_player_node')

        self.declare_parameter('route_file', '~/robotlidar_data/routes/cleaning_route.yaml')
        self.declare_parameter('action_name', 'navigate_through_poses')
        self.declare_parameter('server_wait_timeout_sec', 3.0)
        self.declare_parameter('replay_auxiliary_actions', True)
        self.declare_parameter('start_from_nearest_point', True)
        self.declare_parameter('robot_base_frame', 'base_link')
        self.declare_parameter('localization_timeout_sec', 3.0)
        self.declare_parameter('nearest_start_max_distance_m', 5.0)

        self.route_file = Path(str(self.get_parameter('route_file').value)).expanduser()
        action_name = str(self.get_parameter('action_name').value)
        self.server_wait_timeout = float(self.get_parameter('server_wait_timeout_sec').value)
        self.replay_auxiliary_actions = bool(self.get_parameter('replay_auxiliary_actions').value)
        self.start_from_nearest_point = bool(self.get_parameter('start_from_nearest_point').value)
        self.robot_base_frame = str(self.get_parameter('robot_base_frame').value)
        self.localization_timeout = float(self.get_parameter('localization_timeout_sec').value)
        self.nearest_start_max_distance = float(
            self.get_parameter('nearest_start_max_distance_m').value
        )

        self.action_client = ActionClient(self, NavigateThroughPoses, action_name)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.goal_handle = None
        self.state = 'idle'
        self._active_point_index = -1
        self._active_route_points: list[dict] = []
        self._active_source_indices: list[int] = []

        self.state_publisher = self.create_publisher(String, '/route/player_state', 10)
        self.actuator_publisher = self.create_publisher(Int8, '/actuator/command', 20)
        self.brush_publisher = self.create_publisher(Float32, '/brush/command', 20)
        self.aux_motor_publisher = self.create_publisher(Float32, '/aux_motor/command', 20)

        self.create_service(Trigger, '/route/play', self._play)
        self.create_service(Trigger, '/route/cancel', self._cancel)
        self.create_service(Trigger, '/route/reload', self._reload)

        self.route_data: dict = {}
        self._load_route(log_missing=False)
        self._publish_state('idle')
        self._stop_auxiliaries()

    def _load_route(self, log_missing: bool = True) -> None:
        if not self.route_file.exists():
            self.route_data = {}
            if log_missing:
                raise FileNotFoundError(self.route_file)
            self.get_logger().warning(f'Route file does not exist yet: {self.route_file}')
            return

        data = yaml.safe_load(self.route_file.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('route YAML root must be a mapping')
        points = data.get('points')
        if not isinstance(points, list) or not points:
            raise ValueError('route YAML has no points')
        for index, point in enumerate(points):
            if not isinstance(point, dict):
                raise ValueError(f'route point {index} is not a mapping')
            for key in ('x', 'y', 'yaw'):
                if key not in point:
                    raise ValueError(f'route point {index} has no {key}')

        self.route_data = data
        action_points = sum(1 for point in points if isinstance(point.get('actions'), dict))
        self.get_logger().info(
            f'Loaded route with {len(points)} points, {action_points} action points: {self.route_file}'
        )

    def _reload(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.goal_handle is not None:
            response.success = False
            response.message = 'Cannot reload while route is active'
            return response
        try:
            self._load_route()
        except Exception as exc:
            response.success = False
            response.message = f'Route reload failed: {exc}'
            return response
        response.success = True
        response.message = f"Loaded {len(self.route_data['points'])} route points"
        return response

    def _localized_xy(self, frame_id: str) -> tuple[float, float]:
        try:
            transform = self.tf_buffer.lookup_transform(
                frame_id,
                self.robot_base_frame,
                Time(),
                timeout=Duration(seconds=self.localization_timeout),
            )
        except TransformException as exc:
            raise RuntimeError(
                f'Robot is not localized yet: no TF {frame_id}->{self.robot_base_frame}: {exc}'
            ) from exc
        return (
            float(transform.transform.translation.x),
            float(transform.transform.translation.y),
        )

    @staticmethod
    def _nearest_point_index(points: list[dict], x: float, y: float) -> tuple[int, float]:
        best_index = 0
        best_distance_sq = float('inf')
        for index, point in enumerate(points):
            dx = float(point['x']) - x
            dy = float(point['y']) - y
            distance_sq = dx * dx + dy * dy
            if distance_sq < best_distance_sq:
                best_distance_sq = distance_sq
                best_index = index
        return best_index, math.sqrt(best_distance_sq)

    def _play(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.goal_handle is not None or self.state in ('sending', 'running'):
            response.success = False
            response.message = 'A route is already active'
            return response

        try:
            self._load_route()
        except Exception as exc:
            response.success = False
            response.message = f'Cannot load route: {exc}'
            return response

        if not self.action_client.wait_for_server(timeout_sec=self.server_wait_timeout):
            response.success = False
            response.message = 'Nav2 navigate_through_poses server is unavailable'
            return response

        frame_id = str(self.route_data.get('frame_id', 'map'))
        all_points = list(self.route_data['points'])
        start_index = 0
        nearest_distance = 0.0

        if self.start_from_nearest_point:
            try:
                robot_x, robot_y = self._localized_xy(frame_id)
            except Exception as exc:
                response.success = False
                response.message = str(exc)
                self._publish_state('waiting_localization')
                return response

            start_index, nearest_distance = self._nearest_point_index(
                all_points, robot_x, robot_y
            )
            if (
                self.nearest_start_max_distance > 0.0
                and nearest_distance > self.nearest_start_max_distance
            ):
                response.success = False
                response.message = (
                    f'Nearest route point is {nearest_distance:.2f} m away, '
                    f'limit is {self.nearest_start_max_distance:.2f} m'
                )
                self._publish_state('too_far_from_route')
                return response

        self._active_route_points = all_points[start_index:]
        self._active_source_indices = list(range(start_index, len(all_points)))
        if not self._active_route_points:
            response.success = False
            response.message = 'No route points remain from selected start point'
            return response

        now = self.get_clock().now().to_msg()
        poses = [
            self._make_pose(frame_id, now, point)
            for point in self._active_route_points
        ]

        self._stop_auxiliaries()
        self._active_point_index = -1

        goal = NavigateThroughPoses.Goal()
        goal.poses = poses
        self._publish_state('sending')
        future = self.action_client.send_goal_async(goal, feedback_callback=self._feedback)
        future.add_done_callback(self._goal_response)

        response.success = True
        if self.start_from_nearest_point:
            response.message = (
                f'Starting from nearest route point {start_index + 1}/{len(all_points)} '
                f'({nearest_distance:.2f} m away); sending {len(poses)} points to Nav2'
            )
            self.get_logger().info(response.message)
        else:
            response.message = f'Sending {len(poses)} route points to Nav2'
        return response

    def _goal_response(self, future) -> None:
        try:
            goal_handle = future.result()
        except Exception:
            self.goal_handle = None
            self._stop_auxiliaries()
            self._publish_state('error')
            self.get_logger().exception('Failed to send route goal')
            return

        if not goal_handle.accepted:
            self.goal_handle = None
            self._stop_auxiliaries()
            self._publish_state('rejected')
            self.get_logger().error('Nav2 rejected the route')
            return

        self.goal_handle = goal_handle
        self._publish_state('running')
        self.get_logger().info('Nav2 accepted the route')
        if self._active_source_indices:
            self._apply_actions_for_source_point(self._active_source_indices[0])
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self._result)

    def _feedback(self, feedback_message) -> None:
        feedback = feedback_message.feedback
        poses_remaining = getattr(feedback, 'number_of_poses_remaining', None)
        if poses_remaining is None or not self._active_route_points:
            return

        active_index = len(self._active_route_points) - int(poses_remaining)
        active_index = max(0, min(len(self._active_route_points) - 1, active_index))
        if active_index != self._active_point_index:
            self._active_point_index = active_index
            source_index = self._active_source_indices[active_index]
            self._apply_actions_for_source_point(source_index)
            self.get_logger().debug(
                f'Route point {source_index + 1}/{len(self.route_data.get("points", []))}, '
                f'poses remaining={poses_remaining}'
            )

    def _apply_actions_for_source_point(self, point_index: int) -> None:
        points = self.route_data.get('points', [])
        if not points or point_index < 0 or point_index >= len(points):
            return
        if not self.replay_auxiliary_actions:
            return

        actions = points[point_index].get('actions')
        if not isinstance(actions, dict):
            self._stop_auxiliaries()
            return

        actuator_value = int(actions.get('actuator', 0))
        actuator = Int8()
        actuator.data = 1 if actuator_value > 0 else (-1 if actuator_value < 0 else 0)
        self.actuator_publisher.publish(actuator)

        brush_value = float(actions.get('brush', 0.0))
        if not math.isfinite(brush_value):
            brush_value = 0.0
        brush = Float32()
        brush.data = max(0.0, min(1.0, brush_value))
        self.brush_publisher.publish(brush)

        aux_value = float(actions.get('aux_motor', 0.0))
        if not math.isfinite(aux_value):
            aux_value = 0.0
        aux = Float32()
        aux.data = max(-1.0, min(1.0, aux_value))
        self.aux_motor_publisher.publish(aux)

    def _stop_auxiliaries(self) -> None:
        actuator = Int8()
        actuator.data = 0
        self.actuator_publisher.publish(actuator)
        brush = Float32()
        brush.data = 0.0
        self.brush_publisher.publish(brush)
        aux = Float32()
        aux.data = 0.0
        self.aux_motor_publisher.publish(aux)

    def _result(self, future) -> None:
        try:
            wrapped_result = future.result()
            status = wrapped_result.status
        except Exception:
            self._publish_state('error')
            self.get_logger().exception('Route action failed')
        else:
            if status == 4:
                self._publish_state('completed')
                self.get_logger().info('Route completed')
            elif status == 5:
                self._publish_state('canceled')
                self.get_logger().warning('Route canceled')
            else:
                self._publish_state(f'failed:{status}')
                self.get_logger().error(f'Route finished with action status {status}')
        finally:
            self._stop_auxiliaries()
            self._active_point_index = -1
            self._active_route_points = []
            self._active_source_indices = []
            self.goal_handle = None

    def _cancel(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.goal_handle is None:
            response.success = False
            response.message = 'No active route to cancel'
            return response
        self._stop_auxiliaries()
        self.goal_handle.cancel_goal_async()
        response.success = True
        response.message = 'Route cancellation requested; implements stopped'
        self._publish_state('canceling')
        return response

    def _publish_state(self, state: str) -> None:
        self.state = state
        message = String()
        message.data = state
        self.state_publisher.publish(message)

    @staticmethod
    def _make_pose(frame_id: str, stamp, point: dict[str, float]) -> PoseStamped:
        yaw = float(point['yaw'])
        pose = PoseStamped()
        pose.header.frame_id = frame_id
        pose.header.stamp = stamp
        pose.pose.position.x = float(point['x'])
        pose.pose.position.y = float(point['y'])
        pose.pose.orientation.z = math.sin(yaw / 2.0)
        pose.pose.orientation.w = math.cos(yaw / 2.0)
        return pose

    def destroy_node(self) -> bool:
        try:
            self._stop_auxiliaries()
        except Exception:
            pass
        return super().destroy_node()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = RoutePlayerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
