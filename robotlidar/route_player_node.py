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
from rclpy.node import Node
from std_msgs.msg import Float32, Int8, String
from std_srvs.srv import Trigger


class RoutePlayerNode(Node):
    """Send a recorded route to Nav2 and reproduce recorded tool states by waypoint."""

    def __init__(self) -> None:
        super().__init__('route_player_node')

        self.declare_parameter('route_file', '~/robotlidar_data/routes/cleaning_route.yaml')
        self.declare_parameter('action_name', 'navigate_through_poses')
        self.declare_parameter('server_wait_timeout_sec', 3.0)
        self.declare_parameter('replay_auxiliary_actions', True)

        self.route_file = Path(str(self.get_parameter('route_file').value)).expanduser()
        action_name = str(self.get_parameter('action_name').value)
        self.server_wait_timeout = float(self.get_parameter('server_wait_timeout_sec').value)
        self.replay_auxiliary_actions = bool(self.get_parameter('replay_auxiliary_actions').value)

        self.action_client = ActionClient(self, NavigateThroughPoses, action_name)
        self.goal_handle = None
        self.state = 'idle'
        self._active_point_index = -1

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
        now = self.get_clock().now().to_msg()
        poses = [self._make_pose(frame_id, now, point) for point in self.route_data['points']]

        # Always start with tools stopped. First recorded state is applied only once
        # Nav2 accepts the route, so an aborted start cannot leave an implement running.
        self._stop_auxiliaries()
        self._active_point_index = -1

        goal = NavigateThroughPoses.Goal()
        goal.poses = poses
        self._publish_state('sending')
        future = self.action_client.send_goal_async(goal, feedback_callback=self._feedback)
        future.add_done_callback(self._goal_response)

        response.success = True
        response.message = f'Sending {len(poses)} route points to Nav2 with recorded implement actions'
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
        self._apply_actions_for_point(0)
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self._result)

    def _feedback(self, feedback_message) -> None:
        feedback = feedback_message.feedback
        poses_remaining = getattr(feedback, 'number_of_poses_remaining', None)
        if poses_remaining is None:
            return
        points = self.route_data.get('points', [])
        if not points:
            return

        # Nav2 reports remaining poses; convert that to the waypoint currently being
        # traversed. Clamp because different Nav2 versions can report 0 at completion.
        point_index = len(points) - int(poses_remaining)
        point_index = max(0, min(len(points) - 1, point_index))
        if point_index != self._active_point_index:
            self._apply_actions_for_point(point_index)
            self.get_logger().debug(
                f'Route point {point_index + 1}/{len(points)}, poses remaining={poses_remaining}'
            )

    def _apply_actions_for_point(self, point_index: int) -> None:
        points = self.route_data.get('points', [])
        if not points or point_index < 0 or point_index >= len(points):
            return
        self._active_point_index = point_index
        if not self.replay_auxiliary_actions:
            return

        actions = points[point_index].get('actions')
        if not isinstance(actions, dict):
            # Compatibility with old format_version 1/2 routes: keep tools stopped.
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
